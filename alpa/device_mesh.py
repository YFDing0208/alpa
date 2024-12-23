# pylint: disable=protected-access
"""The device mesh runtime that manages buffers and runs computation
distributedly.

The hierarchy of classes defined in this file:

DeviceCluster  (the whole ray cluster)
|
PhysicalDeviceMeshGroup  (multiple device meshes)   ----GPUletsMeshGroup (multiple meshes)
|                                                       |
PhysicalDeviceMesh  (one device mesh)               ----GPUletsMesh (one mesh conpose of GPUlets)
|
MeshHostWorker  (one host in a device mesh)

Besides, we have two additional classes: VirtualPhysicalMesh and
LogicalDeviceMesh. They are only used during compilation time. They are used to
manipulate meshes flexibly without allocating real resources during compilation
time.
"""
from abc import ABC, abstractmethod
import asyncio
from collections import defaultdict, namedtuple
from collections.abc import Iterable
import logging
from operator import attrgetter
import os
import pickle
import shutil
import threading
import time
from typing import Any, List, Union, Sequence, Tuple, Optional

from jax import core, xla, device_put
from jax._src.api import ShapeDtypeStruct
from jax._src.lib import xla_bridge as xb, xla_extension as xe
from jax._src.tree_util import tree_leaves
from jax.abstract_arrays import array_types
from jax.core import ShapedArray
from jax.interpreters import pxla
from jax.interpreters.pxla import (ShardingSpec, _hashable_index,
                                   ShardedDeviceArray, Index)
from jax.lib import xla_client
import jax.numpy as jnp
import numpy as np
import ray
from ray.util.placement_group import remove_placement_group

from alpa import mesh_profiling
import alpa.collective as col
from alpa.global_env import global_config
from alpa.monkey_patch import set_override_backend
from alpa.shard_parallel.auto_sharding import (LogicalDeviceMesh)
from alpa.parallel_plan import PlacementSpec
from alpa.timer import timers, tracer
from alpa.util import (benchmark_func, list_gpu_info, OrderedSet,
                       update_jax_platform, is_ray_node_resource,
                       try_import_ray_worker, create_placement_group,
                       get_bundle_idx, retrieve_placement_group, get_bundle2ip,
                       check_server_port, GPUInfoActor,
                       )

ray_worker = try_import_ray_worker()

if global_config.backend == "gpu" and global_config.has_cuda:
    from alpa.collective import worker_nccl_util

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ReshardingTileSpec = namedtuple("ReshardingTileSpec",
                                ["offset", "rank", "gpu_idx"])
ReshardingSendSpec = namedtuple("ReshardingSendSpec",
                                ["device_id", "tile_spec"])
ReshardingSendTask = namedtuple("ReshardingSendTask",
                                ["tile_specs", "group_name"])
ReshardingRecvSpec = namedtuple("ReshardingRecvSpec",
                                ["device_id", "shape", "dtype", "tile_specs"])
ReshardingRecvTask = namedtuple("ReshardingRecvTask",
                                ["recv_specs", "group_name"])
ReshardingBroadcastSpec = namedtuple("ReshardingBroadcastSpec", [
    "comm_key", "world_size", "devices_ids", "devices_global_rank",
    "tensor_slices", "recv_tile_shape", "dtype"
])
ReshardingBroadcastTask = namedtuple("ReshardingBroadcastTask",
                                     ["broadcast_specs", "group_name"])


##################################################################
# A lookup table for compute capacity of GPUs.
# The key is the GPU name returned by 'nvidia-smi --query-gpu=name'.
##################################################################
# TODO: add more GPUs.

# The value is the compute capacity for 'Single-Precision(FP32) Performance(TFLOPS)'.
ComputeCapacityTable = {
    "NVIDIA GeForce RTX 3090": 35.6,
    "Tesla V100-SXM2-32GB": 15.7,
    "NVIDIA A16": 99.9,
    "Tesla P100-SXM2-16GB": 99.9,
}

# The value is the memory bandwidth (GB/s)'.
MemoryBandwidthTable = {
    "NVIDIA GeForce RTX 3090": 936,
    "Tesla V100-SXM2-32GB": 900,
    "NVIDIA A16": 99.9,
    "Tesla P100-SXM2-16GB": 99.9,
}

# The value is the interconnect bandwidth within a node (GB/s)'.
InterconnectBandwidthTable = {
    "NVIDIA GeForce RTX 3090": 7.41,
    "Tesla V100-SXM2-32GB": 300,
    "NVIDIA A16": 99.9,
    "Tesla P100-SXM2-16GB": 99.9,
}


########################################
# Virtual Device(GPUlet)
########################################
class GPUlet:
    """
        A Virtual GPU.
        we call it as GPUlets.
    """
    gpulet_id = 0

    def __init__(self, memory: float, belong_to):
        """belong_to 表示该GPUlet属于哪个物理GPU"""
        self.gpulet_id = GPUlet.gpulet_id
        GPUlet.gpulet_id += 1

        self.belong_to = belong_to
        self.memory = memory


    def print_info(self):
        print(f"        GPUlets ID: {self.gpulet_id}, Memory: {self.memory} GB")
        print(f"        belongs to {self.belong_to.gpu_name}: {self.belong_to.uuid}")








########################################
# Physical Device
########################################
class PhysicalGPU:
    """
        A Physical GPU.
    """

    gpu_id = 0

    def __init__(self, gpu_name: str, uuid: str, memory: str, node_id: str):
        self.gpu_id = PhysicalGPU.gpu_id
        PhysicalGPU.gpu_id += 1

        self.gpu_name = gpu_name
        self.uuid = uuid
        self.compute_capacity = ComputeCapacityTable[self.gpu_name]
        self.memory = float(float(memory) / 1024)
        self.node_id = node_id
        self.gpulets = []
        self.num_gpulets = 0

    def set_gpulets(self):
        if len(self.gpulets) != 0:  #对于每个GPU，该函数应该只被调用一次
            raise RuntimeError("set_gpulets should be called only once for each physical GPU!")
        elif not global_config.enable_gpulets:
            raise RuntimeError("set_gpulets is NOT allowed when global_config.enable_gpulets is False!")
        self.num_gpulets = int(self.memory // global_config.gpulets_size)
        self.gpulets = [GPUlet(memory=global_config.gpulets_size, belong_to=self) for _ in range(self.num_gpulets)]

    def print_info(self):
        print(f"Physical GPU: {self.gpu_name}")
        print(f"    GPUID: {self.gpu_id}")
        print(f"    UUID: {self.uuid}")
        print(f"    Memory: {self.memory} GB")
        print(f"    Node ID: {self.node_id}")
        print(f"    Compute Capacity: {self.compute_capacity}")
        print(f"    Number of GPUlets: {self.num_gpulets}")
        print("    GPUlets:")
        for gpulet in self.gpulets:
            gpulet.print_info()







########################################
# Ray Workers
########################################
class DaemonMoveWorker:
    """
        A ray actor that moves local checkpoint into the shared
        filesystem in the background.
    """

    def move(self, from_dir: str, to_dir: str):
        os.makedirs(to_dir, exist_ok=True)
        for file in os.listdir(from_dir):
            from_path = os.path.join(from_dir, file)
            to_path = os.path.join(to_dir, file)
            shutil.move(from_path, to_path)

    def sync(self):
        """Noop function used to synchronize."""


class MeshHostWorker:
    """
    A ray actor that manages the xla computation and buffers on a single host.
    """

    def __init__(self, server_address: str, num_hosts: int, host_id: int,
                 mesh_id: int, move_worker: DaemonMoveWorker,
                 runtime_random_seed: int, worker_global_config: dict):
        self.num_hosts = num_hosts
        self.host_id = host_id
        self.mesh_id = mesh_id
        self.move_worker = move_worker
        self.distributed_client = (
            xla_client._xla.get_distributed_runtime_client(
                server_address, host_id, use_coordination_service=False))
        logger.debug(
            f"{host_id}: Trying to connect to xla runtime at {server_address}")
        self.distributed_client.connect()
        logger.debug(
            f"{host_id}: Success to connect to xla runtime at {server_address}")

        # Set global config to follow the driver
        global_config.update_worker_config(worker_global_config)
        if global_config.backend == "gpu":
            self.backend = xla_client.make_gpu_client(self.distributed_client,
                                                      node_id=host_id)
        else:
            raise NotImplementedError(
                f"backend {global_config.backend} is not supported")
        # Monkey patch the backend
        set_override_backend(self.backend)
        self.local_devices = self.backend.local_devices()
        self.num_devices = len(self.local_devices)
        if global_config.enable_overlapping:
            xe.set_num_device_on_host(self.num_devices)

        self.buffers = {}  # Dict[uuid -> Sequence[DeviceArray]]
        self.executables = {}  # Dict[uud -> MeshWorkerExecutable]

        self.send_tasks = {}  # Dict[uuid -> ReshardingSendTask]
        self.recv_tasks = {}  # Dict[uuid -> ReshardingRecvTask]
        self.broadcast_tasks = {}  # Dict[uuid -> BroadcastTask]
        self.broadcast_communicators = {}

        self.data_loaders = {}  # Dict[uuid -> MeshWorkerDataLoader]
        self.data_loader_iters = {}  # Dict[uuid -> iterator]

        self.set_runtime_random_seed(runtime_random_seed)

        if global_config.pipeline_use_signal_send_recv:
            print("Use signal send recv for debugging.")
            self.signal_buffers = []
            for d in self.local_devices:
                jax_tensor = device_put(jnp.ones((1,), dtype=jnp.int8), d)
                self.signal_buffers.append(
                    worker_nccl_util.to_signal_buffer(jax_tensor))

    ##### Buffer Related Functions #####
    def put_buffers(self,
                    uuids: Union[int, Sequence[int]],
                    datas: Sequence[np.ndarray],
                    num_batch=1,
                    batch_dim=0):
        assert len(datas) == self.num_devices
        if not isinstance(uuids, Iterable):
            uuids = [uuids]
        assert len(uuids) == num_batch
        if num_batch > 1:
            split_datas = []
            for data in datas:
                split_buffers = np.split(data, num_batch, batch_dim)
                split_datas.extend(split_buffers)
            datas = split_datas
        arys = [([None] * self.num_devices) for _ in range(num_batch)]
        for i, data in enumerate(datas):
            if data.dtype == np.int64:
                data = data.astype(np.int32)
            device_id, batch_id = divmod(i, num_batch)
            arys[batch_id][device_id] = (self.backend.buffer_from_pyval(
                data, self.local_devices[device_id]))

        for uuid, ary in zip(uuids, arys):
            self.buffers[uuid] = ary

    def shard_and_put_non_zero_buffer(self, uuids: Union[Sequence[int], int],
                                      shape: Sequence[int], dtype: np.dtype,
                                      indices: Sequence, num_batch: int):
        if isinstance(uuids, int):
            uuids = [uuids]
        assert len(uuids) == num_batch
        assert len(indices) == self.num_devices * num_batch
        arys = [([None] * self.num_devices) for _ in range(num_batch)]
        for device_id in range(self.num_devices):
            for b in range(num_batch):
                shard_shape = []
                idx = device_id * num_batch + b
                for j, s in enumerate(indices[idx]):
                    filled_slice = s.indices(shape[j])
                    dim_size = len(range(*filled_slice))
                    shard_shape.append(dim_size)
                arys[b][device_id] = (self.backend.buffer_from_pyval(
                    np.full(shard_shape, 1e-8, dtype),
                    self.local_devices[device_id]))
        for uuid, ary in zip(uuids, arys):
            self.buffers[uuid] = ary

    def _get_buffers_with_local_ids(self, uuid: int, device_ids: Sequence[int]):
        bufs = self.buffers[uuid]
        # TODO(yonghao): sync communication events. Currently it's safe because
        # we never get values immediately after a cross-mesh communication.
        if device_ids is None:
            return map(np.asarray, bufs)
        elif not isinstance(device_ids, Iterable):
            return np.asarray(bufs[device_ids])
        return [np.asarray(bufs[device_id]) for device_id in device_ids]

    def get_buffers(self,
                    uuids: Union[Sequence[int], int],
                    device_indices: Sequence[int] = None):
        if not isinstance(uuids, Iterable):
            return self._get_buffers_with_local_ids(uuids, device_indices)
        if device_indices is not None:
            assert len(uuids) == len(device_indices)
        else:
            device_indices = [None] * len(uuids)
        return [
            self._get_buffers_with_local_ids(uuid, local_ids)
            for uuid, local_ids in zip(uuids, device_indices)
        ]

    def delete_buffers(self, uuids: Union[Sequence[int], int]):
        if isinstance(uuids, Iterable):
            for uuid in uuids:
                del self.buffers[uuid]
        else:
            del self.buffers[uuids]

    def block_until_ready_buffers(self, uuids: Union[Sequence[int], int]):
        # We have to block all buffers to avoid the last operation is
        # cross-mesh resharding(not SPMD)
        if isinstance(uuids, Iterable):
            for uuid in uuids:
                for buf in self.buffers[uuid]:
                    buf.block_until_ready()
        else:
            for buf in self.buffers[uuids]:
                buf.block_until_ready()

    def get_memory_allocated(self):
        self.sync()
        return max(d.memory_allocated() for d in self.local_devices)

    def get_max_memory_allocated(self):
        self.sync()
        return max(d.max_memory_allocated() for d in self.local_devices)

    def get_available_memory(self):
        self.sync()
        return min(d.available_memory() for d in self.local_devices)

    def reset_memory_stats(self):
        self.sync()
        for device in self.local_devices:
            device.clear_memory_stats()

    ##### Executable Related Functions #####
    def put_executable(self, uuid: int,
                       executable_class: "MeshWorkerExecutable", *args):
        self.executables[uuid] = executable_class(self, uuid, *args)

    def delete_executable(self, uuid: int):
        if uuid in self.executables:
            del self.executables[uuid]

    def run_executable(self, uuid: int, *args, **kwargs):
        self.executables[uuid].execute_on_worker(*args, **kwargs)

    def get_exec_hlo_text(self, uuid: int):
        return self.executables[uuid].get_hlo_text()

    def get_exec_total_allocation_size(self, uuid: int):
        return self.executables[uuid].get_total_allocation_size()

    def get_exec_grad_sync_channel_ids(self, uuid: int):
        return self.executables[uuid].grad_sync_channel_ids

    def set_runtime_random_seed(self, seed: int):
        seed = seed + (self.mesh_id << 20 if self.mesh_id else 0)
        for d in self.local_devices:
            d.set_seed(seed)

    ##### Serialization Related Functions #####
    def sync_move_worker(self):
        ray.get(self.move_worker.sync.remote())

    def save_array(self, ckpt_dir: str, local_cache_dir: Union[str, None],
                   uuid: int, device_ids: Sequence[int],
                   shard_indices: Sequence[Index], global_shape: Sequence[int]):
        assert uuid in self.buffers
        array_buffers = self.buffers[uuid]

        shard_names = [
            f"shard_{self.host_id}.{i}" for i in range(len(device_ids))
        ]

        metadata = {
            "global_shape": global_shape,
            "dtype": self.buffers[uuid][0].dtype,
            "shard_names": shard_names,
            "shard_indices": shard_indices,
        }

        # create directories if not exist
        os.makedirs(ckpt_dir, exist_ok=True)
        if local_cache_dir is not None:
            os.makedirs(local_cache_dir, exist_ok=True)
            save_dir = local_cache_dir
        else:
            save_dir = ckpt_dir

        for shard_name, device_id in zip(shard_names, device_ids):
            with open(os.path.join(save_dir, shard_name), "wb") as datafile:
                np.save(datafile, array_buffers[device_id])

        with open(os.path.join(save_dir, f"metadata_{self.host_id}"),
                  "wb") as metafile:
            pickle.dump(metadata, metafile)

        # move data
        if local_cache_dir is not None:
            self.move_worker.move.remote(local_cache_dir, ckpt_dir)

    def load_array(self, ckpt_dir: str, uuid: Sequence[int],
                   device_ids: Sequence[int], shard_indices: Sequence[Index]):
        metadatas = list(
            filter(lambda fname: fname.startswith("metadata"),
                   os.listdir(ckpt_dir)))
        # pylint: disable=import-outside-toplevel
        from alpa.serialization import load_sharded_array
        entire_arr = load_sharded_array(ckpt_dir, metadatas)
        array_buffers = [None] * self.num_devices
        for index, device_id in zip(shard_indices, device_ids):
            data = entire_arr[index]
            if data.dtype == np.int64:
                data = data.astype(np.int32)
            array_buffers[device_id] = (self.backend.buffer_from_pyval(
                data, self.local_devices[device_id]))
        self.buffers[uuid] = array_buffers

    ##### Data loader Related Functions #####
    def put_data_loader(self, uuid: int, *args):
        # pylint: disable=import-outside-toplevel
        from alpa.data_loader import MeshWorkerDataLoader
        self.data_loaders[uuid] = MeshWorkerDataLoader(self, *args)

    def data_loader_iter(self, uuid: int):
        self.data_loader_iters[uuid] = iter(self.data_loaders[uuid])

    def data_loader_next(self, uuid: int):
        next(self.data_loader_iters[uuid])

    def delete_data_loader(self, uuid: int):
        del self.data_loaders[uuid]

    ##### Cross Mesh Resharding Related Functions #####
    @staticmethod
    def init_collective_group(world_size, rank, backend, group_name):
        """Initialize the collective group eagerly."""
        col.init_collective_group(world_size,
                                  rank,
                                  backend=backend,
                                  group_name=group_name)

    @staticmethod
    def generate_nccl_uid(group_name):
        """Generate the NCCL unique ID in advance."""
        g = col.check_and_get_group(group_name)
        uid = g.generate_nccl_uid()
        return uid

    @staticmethod
    def init_p2p_communicator(group_name, my_rank, my_gpu_idx, peer_rank,
                              peer_gpu_idx, nccl_uid):
        """Initialize the P2P communicator from within the mesh workers."""
        assert col.is_group_initialized(group_name)
        assert col.get_rank(group_name) == my_rank
        g = col.check_and_get_group(group_name)
        g.create_p2p_communicator(my_gpu_idx, peer_rank, peer_gpu_idx, nccl_uid)

    @staticmethod
    def init_broadcast_communicator(group_name, comm_key, world_size,
                                    device_ids, devices_global_rank, nccl_uid):
        """Initialize the P2P communicator from within the mesh workers."""
        assert col.is_group_initialized(group_name)
        g = col.check_and_get_group(group_name)
        g.create_nccl_broadcast_communicator(comm_key, world_size, device_ids,
                                             devices_global_rank, nccl_uid)

    @staticmethod
    def destroy_collective_group(group_name: str = "default"):
        col.destroy_collective_group(group_name)

    def create_and_set_cross_mesh_communicators(self, world_size, rank, backend,
                                                group_name, key):
        """Create collective communicators for the cross mesh group."""
        if not col.is_group_initialized(group_name):
            self.init_collective_group(world_size, rank, backend, group_name)
        g = col.check_and_get_group(group_name)
        devices = list(range(self.num_devices))
        g.create_and_set_xla_communicators(devices, key)

    def put_resharding_send_task(self, uuid, tasks, group_name):
        self.send_tasks[uuid] = ReshardingSendTask(tile_specs=tasks,
                                                   group_name=group_name)

    def put_resharding_recv_task(self, uuid, tasks, group_name):
        self.recv_tasks[uuid] = ReshardingRecvTask(recv_specs=tasks,
                                                   group_name=group_name)

    def run_resharding_send_task(self, uuid, ary_uuid):
        task: ReshardingSendTask = self.send_tasks[uuid]
        group_name = task.group_name
        if global_config.enable_overlapping:
            col.wait_events(group_name, [ary_uuid], self.num_devices, True)

        for send_tile_spec in task.tile_specs:
            send_tile_spec: ReshardingSendSpec
            self.send_tile(ary_uuid, send_tile_spec.device_id,
                           send_tile_spec.tile_spec.offset,
                           send_tile_spec.tile_spec.rank,
                           send_tile_spec.tile_spec.gpu_idx, task.group_name)

    def run_resharding_recv_task(self, uuid, ary_uuid, set_empty_buffer=True):
        task: ReshardingRecvTask = self.recv_tasks[uuid]
        group_name = task.group_name
        if set_empty_buffer and ary_uuid not in self.buffers:
            assert not global_config.enable_overlapping, "Unsupported."
            self.buffers[ary_uuid] = [None] * self.num_devices

        if global_config.enable_overlapping:
            col.wait_events(group_name, [ary_uuid], self.num_devices, False)

        buffers = self.buffers[ary_uuid]
        for recv_spec in task.recv_specs:
            recv_spec: ReshardingRecvSpec
            device_id = recv_spec.device_id
            if set_empty_buffer:
                buffers[device_id] = self.backend.buffer_from_pyval(
                    np.full(recv_spec.shape, 1e-8, recv_spec.dtype),
                    self.local_devices[device_id])

            for recv_tile_spec in recv_spec.tile_specs:
                recv_tile_spec: ReshardingTileSpec
                self.recv_tile(ary_uuid, device_id, recv_tile_spec.offset,
                               recv_tile_spec.rank, recv_tile_spec.gpu_idx,
                               task.group_name)

        if global_config.enable_overlapping:
            col.record_events(group_name, [ary_uuid], self.num_devices, False)

    def send_tile(self, uuid: int, device_id: int, offset: Sequence[slice],
                  dst_rank: int, dst_gpu_idx: int, group_name: str):
        if global_config.pipeline_use_signal_send_recv:
            signal = self.signal_buffers[device_id]
            col.send_multigpu(signal,
                              dst_rank,
                              dst_gpu_idx,
                              group_name,
                              start_pos=0,
                              n_elements=1)
        else:
            worker_nccl_util.send_tile(self, uuid, device_id, offset, dst_rank,
                                       dst_gpu_idx, group_name)

    def recv_tile(self, uuid: int, device_id: int,
                  indices_in_dst_tile: Sequence[slice], src_rank: int,
                  src_gpu_idx: int, group_name: str):
        if uuid not in self.buffers:
            raise RuntimeError("Buffer has not been created.")

        if global_config.pipeline_use_signal_send_recv:
            signal = self.signal_buffers[device_id]
            col.recv_multigpu(signal,
                              src_rank,
                              src_gpu_idx,
                              group_name,
                              start_pos=0,
                              n_elements=1)
        else:
            worker_nccl_util.recv_tile(self, uuid, device_id,
                                       indices_in_dst_tile, src_rank,
                                       src_gpu_idx, group_name)

    def put_resharding_broadcast_task(self, uuid, tasks, group_name):
        self.broadcast_tasks[uuid] = ReshardingBroadcastTask(
            broadcast_specs=tasks, group_name=group_name)

    def run_resharding_broadcast_task(self,
                                      uuid,
                                      ary_uuid,
                                      set_empty_buffer=True):
        task: ReshardingBroadcastTask = self.broadcast_tasks[uuid]
        group_name = task.group_name
        broadcast_specs = task.broadcast_specs
        if set_empty_buffer and ary_uuid not in self.buffers:
            assert not global_config.enable_overlapping, "Unsupported."
            picked_spec = list(broadcast_specs.values())[0]
            shape = picked_spec.recv_tile_shape
            dtype = picked_spec.dtype
            self.buffers[ary_uuid] = [
                self.backend.buffer_from_pyval(np.full(shape, 1e-8, dtype),
                                               self.local_devices[device_id])
                for device_id in range(self.num_devices)
            ]

        has_recv = False
        for group_idx in broadcast_specs:
            broadcast_spec: ReshardingBroadcastSpec = broadcast_specs[group_idx]
            is_send = broadcast_spec.devices_global_rank[0] == 0
            has_recv = has_recv or not is_send
            if global_config.enable_overlapping:
                col.wait_events(group_name, [ary_uuid], self.num_devices,
                                is_send)

            worker_nccl_util.broadcast(self, ary_uuid, broadcast_spec.comm_key,
                                       broadcast_spec.world_size,
                                       broadcast_spec.devices_ids,
                                       broadcast_spec.devices_global_rank,
                                       broadcast_spec.tensor_slices,
                                       task.group_name)
        if global_config.enable_overlapping and has_recv:
            col.record_events(group_name, [ary_uuid], self.num_devices, False)

    ##### Profiling and Debugging Related Functions #####
    def profile_hlo_ops(self, op_infos: Sequence[Any], cache_filename: str,
                        single_timeout: float):
        num_devices = self.num_hosts * len(self.local_devices)
        return mesh_profiling.profile_hlo_ops(op_infos, self.backend,
                                              self.local_devices, self.host_id,
                                              num_devices, cache_filename,
                                              single_timeout)

    def profile_executable_with_dummy_inputs(self, uuid: int, **kwargs):
        return self.executables[uuid].profile_with_dummy_inputs(
            self.backend, self.local_devices, **kwargs)

    def profile_resharding_send_task(self,
                                     uuid,
                                     buf_uuids,
                                     warmup=1,
                                     repeat=3,
                                     number=3,
                                     sync=False):
        # TODO(yonghao): the sync function should be carefully reconsidered
        def run_fn():
            self.run_resharding_send_task(uuid, buf_uuids)

        sync_fn = self.sync if sync else None
        costs = benchmark_func(run_fn, sync_fn, warmup, repeat, number)
        return np.mean(costs)

    def profile_resharding_recv_task(self,
                                     uuid,
                                     buf_uuids,
                                     warmup=1,
                                     repeat=3,
                                     number=3,
                                     sync=False):
        set_empty_buffer = True

        def run_fn():
            nonlocal set_empty_buffer
            self.run_resharding_recv_task(uuid, buf_uuids, set_empty_buffer)
            set_empty_buffer = False

        sync_fn = self.sync if sync else None
        costs = benchmark_func(run_fn, sync_fn, warmup, repeat, number)
        return np.mean(costs)

    @staticmethod
    def get_timer(name: str):
        return timers(name)

    @staticmethod
    def reset_timer(name: str):
        timers(name).reset()

    @staticmethod
    def get_tracer():
        return tracer

    def get_live_buffer_uuids(self):
        return list(self.buffers.keys())

    ##### Other Functions #####
    def sync(self, sync_all_devices=False):
        # We sync one device instead of all for smaller runtime overhead.
        # This is correct because of SPMD.
        if sync_all_devices:
            for device in self.local_devices:
                device.synchronize_all_activity()
        else:
            self.local_devices[0].synchronize_all_activity()

    def sync_all(self):
        for device in self.local_devices:
            device.synchronize_all_activity()

    @staticmethod
    def check_alive():
        return True

    def shutdown(self):
        self.sync()
        self.buffers.clear()
        self.executables.clear()
        self.distributed_client.shutdown()
        # sync & shutdown DaemonMoveWorker
        self.sync_move_worker()
        ray.kill(self.move_worker)
        self.move_worker = None


########################################
# DeviceMeshs
########################################
class PhysicalDeviceMesh(ABC):
    """The base class of physical device mesh.

    A physical device mesh is a 2-dimensional mesh that runs SPMD computation on
    all devices in the mesh.
    """

    num_hosts: int
    num_devices_per_host: int
    mesh_id: int
    operation_executables: dict
    one_replica_ids: dict

    def get_signature(self) -> str:
        """Return a signature string that contains the mesh shape and GPU
        model."""
        gpu_type = list_gpu_info()
        gpu_name = gpu_type.split("\n")[0].split(" (UUID:")[0][7:]
        ret = f"{self.num_hosts},{self.num_devices_per_host},{gpu_name}"
        ret = ret.replace(" ", "-")
        return ret

    def _compute_one_replica_ids(self, indices, aval_shape, sharding_spec):
        # Tuple (aval_shape, sharding_spec) is 1-1 mapped to indices
        # used to compute one_replica_ids
        if (aval_shape, sharding_spec) in self.one_replica_ids:
            return self.one_replica_ids[(aval_shape, sharding_spec)]

        one_replica_indices = []
        one_replica_host_local_ids = []
        seen_index_hashes = set()
        for i, index in enumerate(indices):
            hashed_index = _hashable_index(index)
            if hashed_index not in seen_index_hashes:
                one_replica_indices.append(i)
                one_replica_host_local_ids.append(
                    divmod(i, self.num_devices_per_host))
                seen_index_hashes.add(hashed_index)
        self.one_replica_ids[(
            aval_shape,
            sharding_spec)] = one_replica_indices, one_replica_host_local_ids
        return one_replica_indices, one_replica_host_local_ids

    @property
    def shape(self):
        return self.num_hosts, self.num_devices_per_host

    @property
    def num_devices(self):
        """Return the total number of GPUs on this mesh."""
        return self.num_hosts * self.num_devices_per_host

    ##### Logical Mesh Related Functions #####
    def get_logical_mesh(self,
                         mesh_shape: Optional[Sequence[int]] = None,
                         mesh_alpha: Optional[float] = None,
                         mesh_beta: Optional[float] = None,
                         mesh_topology: Optional[str] = None,
                         intra_host_bandwidth: Optional[float] = None,
                         inter_host_bandwidth: Optional[float] = None):
        """
        Return a logical mesh and parameters of the alpha-beta communication
        cost model. The logical view is used for auto-sharding.
        """
        if mesh_shape is None:
            mesh_shape = (self.num_hosts, self.num_devices_per_host)

        id_mesh = np.arange(self.num_devices).reshape(mesh_shape)

        if mesh_topology is None:
            # Use the provided mesh_alpha and mesh_beta
            mesh_alpha = mesh_alpha or (1, 1)
            mesh_beta = mesh_beta or (1, 0.1)
        elif mesh_topology == "tree":
            # Derive mesh_alpha and mesh_beta from topology,
            # intra_host_bandwidth and inter_host_bandwidth
            assert mesh_alpha is None
            assert mesh_beta is None
            mesh_alpha = [1] * 2
            mesh_beta = [None] * 2
            host_ids = np.tile(
                np.arange(self.num_hosts).reshape(-1, 1),
                self.num_devices_per_host)
            host_ids = host_ids.reshape(mesh_shape)

            # Compute bandwidth of doing communication along dim 0.
            # 1. Compute the number of links between each host pairs.
            #    Assume using ring-based algorithms.
            host_link_ct = defaultdict(int)
            for j in range(mesh_shape[1]):
                for i in range(mesh_shape[0]):
                    left = host_ids[i][j]
                    right = host_ids[(i + 1) % mesh_shape[0]][j]
                    if left != right:
                        if left > right:
                            left, right = right, left
                        host_link_ct[(left, right)] += 1

            j = 0
            # 2. Bandwidth between two hosts
            #    = total_bandwidth / number_of_links.
            #    Bandwdith along a communication dimension
            #    = min bandwidth of all links.
            bandwidth = intra_host_bandwidth
            for i in range(mesh_shape[0]):
                left = host_ids[i][j]
                right = host_ids[(i + 1) % mesh_shape[0]][j]
                if left != right:
                    if left > right:
                        left, right = right, left
                    bandwidth = min(
                        bandwidth,
                        inter_host_bandwidth / host_link_ct[(left, right)])
            mesh_beta[0] = 1 / bandwidth

            # Compute bandwidth of doing communication along dim 1.
            host_link_ct = defaultdict(int)
            for i in range(mesh_shape[0]):
                for j in range(mesh_shape[1]):
                    left = host_ids[i][j]
                    right = host_ids[i][(j + 1) % mesh_shape[1]]
                    if left != right:
                        if left > right:
                            left, right = right, left
                        host_link_ct[(left, right)] += 1

            i = 0
            bandwidth = intra_host_bandwidth
            for j in range(mesh_shape[1]):
                left = host_ids[i][j]
                right = host_ids[i][(j + 1) % mesh_shape[1]]
                if left != right:
                    if left > right:
                        left, right = right, left
                    bandwidth = min(
                        bandwidth,
                        inter_host_bandwidth / host_link_ct[(left, right)])
            mesh_beta[1] = 1 / bandwidth

        return LogicalDeviceMesh(self, id_mesh, mesh_alpha, mesh_beta)

    ##### Executable Related Functions #####
    @abstractmethod
    def shard_args_to_bufs(self, shard_indices: Sequence[Sequence[Index]],
                           donated_invars: Sequence[bool],
                           batch_invars: Sequence[bool], num_micro_batches: int,
                           args: Sequence[Any]):
        """Shard high-level arguments as low-level buffers."""
        raise NotImplementedError()

    @abstractmethod
    def shard_args_to_arrays(self, avals: Sequence[ShapedArray],
                             shard_indices: Sequence[Sequence[Index]],
                             sharding_specs: Sequence[ShardingSpec],
                             args: Sequence[Any]):
        """Shard arguments (np.ndarray) as distributed arrays."""
        raise NotImplementedError()

    def shard_args_to_arrays_ps(self, placement_specs: PlacementSpec,
                                args: Sequence[Any]):
        """
        Shard arguments (np.ndarray) as distributed arrays according to
        PlacementSpec.
        """
        avals = tuple(x.aval for x in placement_specs)
        assert all(
            len(x.mesh_ids) == 1 and x.mesh_ids[0] == self.mesh_id
            for x in placement_specs)
        specs = tuple(x.sharding_specs[0] for x in placement_specs)
        indices = tuple(
            pxla.spec_to_indices(aval.shape, spec)
            for aval, spec in zip(avals, specs))
        return self.shard_args_to_arrays(avals, indices, specs, args)

    @abstractmethod
    def get_outputs_handler(self, avals: Sequence[ShapedArray],
                            sharding_specs: Sequence[ShardingSpec]):
        """
        Get a function that wraps low-level buffers to high-level output arrays.
        """
        raise NotImplementedError()

    @abstractmethod
    def set_runtime_random_seed(self, seed: int):
        raise NotImplementedError()

    ##### Profiling Related Functions #####
    @abstractmethod
    def get_remote_timer(self, timer_name: str):
        raise NotImplementedError()

    @abstractmethod
    def reset_remote_timer(self, timer_name: str):
        raise NotImplementedError()

    @abstractmethod
    def get_remote_tracer(self):
        raise NotImplementedError()

    @abstractmethod
    def get_memory_allocated(self):
        raise NotImplementedError()

    @abstractmethod
    def get_max_memory_allocated(self):
        raise NotImplementedError()

    @abstractmethod
    def get_available_memory(self):
        raise NotImplementedError()

    @abstractmethod
    def reset_memory_stats(self):
        raise NotImplementedError()

    ##### Other Functions #####
    @abstractmethod
    def sync_workers(self):
        """Sync device activities on all workers."""
        raise NotImplementedError()

    @abstractmethod
    def shutdown(self, forced=False):
        """Shut down the mesh."""
        raise NotImplementedError()


class LocalPhysicalDeviceMesh(PhysicalDeviceMesh):
    """
    A single-host physical device mesh to run computation on local devices.
    It uses the native XLA runtime.
    """

    def __init__(self, devices: Sequence["Device"] = None):
        self.devices = devices if devices is not None else xb.local_devices()
        self.num_hosts = 1
        self.num_devices_per_host = len(self.devices)
        self.mesh_id = -1
        self.device_strs = []
        self.operation_executables = {}
        self.one_replica_ids = {}

        self.backend = xb.get_backend(global_config.backend)

        self.set_runtime_random_seed(global_config.runtime_random_seed)

    ##### Executable Related Functions #####
    def shard_args_to_bufs(self, shard_indices: Sequence[Sequence[Index]],
                           donated_invars: Sequence[bool],
                           batch_invars: Sequence[bool], num_micro_batches: int,
                           args: Sequence[Any]):
        bufs = []
        for arg, indices, donated, is_batch_var in zip(args, shard_indices,
                                                       donated_invars,
                                                       batch_invars):
            if is_batch_var:
                micro_batches = jnp.split(arg, num_micro_batches)
                bufs.append([
                    pxla._shard_arg(x, self.devices, indices, None)
                    for x in micro_batches
                ])
            else:
                if (isinstance(arg, pxla.ShardedDeviceArray) and
                        arg.indices == indices):
                    bufs.append(arg.device_buffers)
                else:
                    bufs.append(
                        pxla._shard_arg(arg, self.devices, indices, None))

            if isinstance(arg, xe.DeviceArray) and donated:
                arg.delete()

        return bufs

    def shard_args_to_arrays(self, avals: Sequence[ShapedArray],
                             shard_indices: Sequence[Sequence[Index]],
                             sharding_specs: Sequence[ShardingSpec],
                             args: Sequence[Any]):
        arrays = []
        for i in range(len(avals)):
            if global_config.use_dummy_value_for_benchmarking:
                args[i] = np.full(avals[i].shape, 1e-8, avals[i].dtype)
            shards = [
                args[i][shard_indices[i][k]] for k in range(len(self.devices))
            ]
            buffers = [device_put(x, d) for x, d in zip(shards, self.devices)]
            arrays.append(
                pxla._ShardedDeviceArray(avals[i], sharding_specs[i], buffers,
                                         shard_indices[i]))
        return arrays

    def get_outputs_handler(self, avals: Sequence[ShapedArray],
                            sharding_specs: Sequence[ShardingSpec]):
        pmap_specs = pxla._get_pmap_sharding(np.arange(self.num_devices),
                                             sharding_specs)
        outs_handler = pxla.local_avals_to_results_handler(avals, pmap_specs)
        return outs_handler

    def set_runtime_random_seed(self, seed: int):
        for d in self.devices:
            if d is not None:
                d.set_seed(seed)

    ##### Profiling Related Functions #####
    def get_remote_timer(self, timer_name: str):
        return timers(timer_name)

    def reset_remote_timer(self, timer_name: str):
        timers(timer_name).reset()

    def get_remote_tracer(self):
        return tracer

    def get_memory_allocated(self):
        return max(d.memory_allocated() for d in self.devices)

    def get_max_memory_allocated(self):
        return max(d.max_memory_allocated() for d in self.devices)

    def get_available_memory(self):
        return min(device.available_memory() for device in self.devices)

    def reset_memory_stats(self):
        for device in self.devices:
            device.clear_memory_stats()

    ##### Other Functions #####
    def sync_workers(self):
        # We sync one device instead of all for smaller runtime overhead.
        # This is correct because of SPMD.
        self.devices[0].synchronize_all_activity()

    def shutdown(self, forced=False):
        self.sync_workers()
        self.operation_executables.clear()


def device_id_to_str(host_ip, device_id, device_type="gpu"):
    """Convert device id (int) to a canonical device string."""
    return f"{host_ip}:{device_type}:{device_id}"


# Used ports for XLA distributed runtime servers.
used_port_set = set((None,))


class DistributedPhysicalDeviceMesh(PhysicalDeviceMesh):
    """
    A multi-host physical device mesh to run computation distributedly.
    It uses ray actors and the distributed XLA runtime.
    """

    def __init__(self,
                 host_ids: Sequence[int],
                 host_info: Sequence[dict],
                 num_devices_per_host: int,
                 parent: Optional["VirtualPhysicalMesh"] = None,
                 devices: Optional[Sequence[Sequence[int]]] = None,
                 mesh_id: Optional[int] = None,
                 namespace: Optional[str] = None):
        # host_ids are the indices of hosts in the global DeviceCluster
        self.host_ids = host_ids
        self.host_info = host_info
        self.num_hosts = len(host_ids)
        self.num_devices_per_host = num_devices_per_host
        self.parent = parent
        self.mesh_id = mesh_id
        self.workers = None
        self.service_server = None
        self.operation_executables = {}
        self.one_replica_ids = {}
        self.namespace = namespace

        if devices is not None:
            if len(devices) != len(host_ids):
                raise RuntimeError(
                    "Please specify the gpu IDs used on each host.")
            if not all(len(ids) == num_devices_per_host for ids in devices):
                raise RuntimeError(
                    "Devices specified for each host does not align "
                    "with `num_devices_per_host`.")
        else:
            devices = [list(range(num_devices_per_host)) for _ in host_ids]

        self.devices = devices
        self.device_strs = []
        self.node_ips = []
        for i in range(self.num_hosts):
            ip = self.host_info[i]["NodeManagerAddress"]
            self.device_strs.extend(
                [device_id_to_str(ip, j) for j in devices[i]])
            self.node_ips.append(ip)

        found_existing_workers = False
        if self.namespace:
            try:
                ray.get_actor(self.get_host_worker_name(0))
                found_existing_workers = True
            except ValueError:
                pass

        if found_existing_workers:
            self.service_server = None
            self.workers = self.connect_to_existing_workers()
            self.launched = False
        else:
            self.service_server, self.workers = self.launch_xla_servers()
            logger.info(f"Launching workers on hosts: {self.host_ids}")
            self.launched = True

        self.to_delete_remote_refs = []
        self.to_delete_remote_ref_ct = 0

    def get_host_worker_name(self, host_id):
        if self.namespace:
            return f"mesh_{self.mesh_id}_host_{host_id}"
        else:
            return None

    def connect_to_existing_workers(self):
        workers = []
        for i in range(self.num_hosts):
            workers.append(ray.get_actor(self.get_host_worker_name(i)))
        return workers

    def launch_xla_servers(self):
        # Launch distributed xla runtime
        port = None
        while port in used_port_set:
            port = np.random.randint(global_config.xla_server_port_start,
                                     global_config.xla_server_port_end)
            if check_server_port(ray.util.get_node_ip_address(), port):
                port = None
        used_port_set.add(port)

        server_address = f"{ray.util.get_node_ip_address()}:{port}"
        logger.debug(f"Trying to start XLA gRPC server on port: {port}...")
        service_server = xla_client._xla.get_distributed_runtime_service(
            server_address, self.num_hosts, use_coordination_service=False)
        logger.debug(f"Success to start XLA gRPC server on port: {port}...")
        time.sleep(0.4)

        # Launch workers
        workers = []

        # retrieve the placement group
        placement_group = retrieve_placement_group()

        # get the sorted bundle index list
        device_bundle_idx_list = get_bundle_idx(placement_group, self.node_ips)

        for i in range(self.num_hosts):
            # Set XLA environment variables
            env_vars = {
                "ALPA_IS_WORKER":
                    "True",
                "NCCL_USE_MULTISTREAM":
                    "False",
                "XLA_PYTHON_CLIENT_MEM_FRACTION":
                    str(global_config.xla_client_mem_fraction),
                "XLA_FLAGS": (os.environ.get("XLA_FLAGS", "") +
                              f" --xla_gpu_autotune_level"
                              f"={global_config.xla_gpu_autotune_level}"),
                "XLA_PYTHON_CLIENT_PREALLOCATE":
                    global_config.xla_client_client_preallocate,
                # "NCCL_LAUNCH_MODE": "PARALLEL",
                # "XLA_FLAGS": "--xla_dump_to=hlo --xla_dump_hlo_pass_re=.*"
                # "NCCL_DEBUG": "INFO" if i == 0 else "VERSION",
                # "NCCL_DEBUG_SUBSYS": "ALL",
                # "RAY_IGNORE_UNHANDLED_ERRORS": "True",
            }

            if global_config.resharding_mode == "broadcast":
                env_vars["NCCL_ALGO"] = "Ring"
                env_vars["NCCL_PROTO"] = "Simple"

            if "XLA_PYTHON_CLIENT_ALLOCATOR" in os.environ:
                env_vars["XLA_PYTHON_CLIENT_ALLOCATOR"] = os.environ[
                    "XLA_PYTHON_CLIENT_ALLOCATOR"]

            if "NCCL_DEBUG" in os.environ:
                env_vars["NCCL_DEBUG"] = os.environ[
                    "NCCL_DEBUG"] if i == 0 else "VERSION"

            if global_config.use_aws_efa:
                env_vars.update({
                    "FI_PROVIDER": "efa",
                    "FI_EFA_USE_DEVICE_RDMA": "1",
                    "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH",
                                                      ""),  # For libnccl-net.so
                    "NCCL_PROTO": "simple",
                })

            bundle_index = device_bundle_idx_list[i]

            host_worker_name = self.get_host_worker_name(i)

            # Launch the DaemonMoveWorker
            cls = ray.remote(num_cpus=0)(DaemonMoveWorker)
            move_worker = cls.options(
                placement_group=placement_group,
                placement_group_bundle_index=bundle_index).remote()

            # Launch the MeshHostWorker
            logger.info(f"num_gpus: {self.num_devices_per_host}")
            cls = ray.remote(num_cpus=0,
                             num_gpus=self.num_devices_per_host)(MeshHostWorker)
            worker = cls.options(placement_group=placement_group,
                                 placement_group_bundle_index=bundle_index,
                                 name=host_worker_name,
                                 runtime_env={
                                     "env_vars": env_vars
                                 }).remote(server_address, self.num_hosts, i,
                                           self.mesh_id, move_worker,
                                           global_config.runtime_random_seed,
                                           global_config)
            workers.append(worker)
        return service_server, workers

    @property
    def host_ips(self):
        ips = [
            self.host_info[i]["NodeManagerAddress"]
            for i, _ in enumerate(self.host_ids)
        ]
        return ips

    def get_virtual_physical_mesh(self):
        return VirtualPhysicalMesh(
            host_ids=self.host_ids,
            host_info=self.host_info,
            num_devices_per_host=self.num_devices_per_host,
            parent=self,
            devices=self.devices)

    def _split_ids_to_host(self, host_local_ids: Sequence[Tuple[int, int]]):
        if host_local_ids is None:
            full_local_id = [
                range(self.num_devices_per_host) for _ in range(self.num_hosts)
            ]
            full_id_local_idx = [(i, j)
                                 for i in range(self.num_hosts)
                                 for j in range(self.num_devices_per_host)]
            return tuple(full_local_id), full_id_local_idx
        per_host_id = [[] for _ in range(self.num_hosts)]
        host_id_local_idx = []
        for id_pair in host_local_ids:
            host_id, device_id = id_pair
            host_id_local_idx.append((host_id, len(per_host_id[host_id])))
            per_host_id[host_id].append(device_id)
        return per_host_id, host_id_local_idx

    ##### Buffer Related Functions #####
    def get_remote_buffers(
            self,
            ary_refs: Union[List["RemoteArrayRef"], "RemoteArrayRef"],
            host_local_ids: Sequence[Sequence[Tuple[int, int]]] = None,
            batching=False,
            return_ray_ref=False):
        """
        Get values of remote buffers.

        Args:
            host_local_ids: For each RemoteArrayRef, we can fetch a list of
              buffers from multiple devices on multiple hosts. This variable
              defines a list of (host_id, local_id) pair for each
              RemoteArrayRef. If it is None, fetch all remote buffers.
            batching: Whether batch remote calls by host ids. This can reduce
              ray overhead.
        """
        return_list = True
        if not isinstance(ary_refs, Iterable):
            return_list = False
            ary_refs = [ary_refs]
        if host_local_ids is None:
            host_local_ids = [None] * len(ary_refs)
        elif not isinstance(host_local_ids, Iterable):
            assert not return_list
            host_local_ids = [host_local_ids]

        if batching:
            # Batch the remote calls by host ids
            ary_ids = np.array([ref.uuid for ref in ary_refs])
            per_host_ids = np.empty((self.num_hosts, len(ary_ids)),
                                    dtype=object)
            host_id_local_indices = []
            for arg_id, id_pairs in enumerate(host_local_ids):
                tmp_ids, tmp_indices = self._split_ids_to_host(id_pairs)
                host_id_local_indices.append(tmp_indices)
                for host_id, tmp_per_host in enumerate(tmp_ids):
                    per_host_ids[host_id][arg_id] = np.array(tmp_per_host)

            # [host_id-> (buf_idx-> (local_device_id->device_buffer))]
            obj_refs = []
            for host_id in range(self.num_hosts):
                obj_refs.append(self.workers[host_id].get_buffers.remote(
                    ary_ids, per_host_ids[host_id]))
            per_host_results = ray.get(obj_refs)
            # [buf_id -> (flatten_id -> device_buffer)]
            ret = []
            for ref_idx, id_pairs in enumerate(host_id_local_indices):
                buffers = []
                for id_pair in id_pairs:
                    host_id, local_idx = id_pair
                    buffers.append(
                        per_host_results[host_id][ref_idx][local_idx])
                ret.append(buffers)
        else:
            obj_refs = []
            for ary_ref, id_pairs in zip(ary_refs, host_local_ids):
                ary_obj_refs = []
                for id_pair in id_pairs:
                    host_id, local_id = id_pair
                    ary_obj_refs.append(
                        self.workers[host_id].get_buffers.remote(
                            ary_ref.uuid, local_id))
                obj_refs.append(ary_obj_refs)
            if return_ray_ref:
                ret = obj_refs
            else:
                ret = [ray.get(refs) for refs in obj_refs]
        return ret if return_list else ret[0]

    def delete_remote_buffers(self, ary_refs: List["RemoteArrayRef"]):
        """Delete remote buffers."""
        if not self.workers or not ray or not ray_worker or not np.array:
            return

        # Put delete requests into a buffer
        for ary_ref in ary_refs:
            self.to_delete_remote_refs.append(ary_ref.uuid)
        self.to_delete_remote_ref_ct += len(ary_refs)

        # Execute the delete requests if there are enough requests
        if (self.to_delete_remote_ref_ct >
                global_config.delete_remote_arrays_threshold):
            to_delete_remote_refs = np.array(self.to_delete_remote_refs)
            try:
                for host_id in range(self.num_hosts):
                    self.workers[host_id].delete_buffers.remote(
                        to_delete_remote_refs)
            except AttributeError:
                pass
            self.to_delete_remote_refs = []
            self.to_delete_remote_ref_ct = 0

    def block_until_ready_remote_buffers(self,
                                         ary_refs: List["RemoteArrayRef"]):
        """Block until the remote buffers are ready."""
        tasks = []
        ary_uuids = np.array([ref.uuid for ref in ary_refs])
        for worker in self.workers:
            tasks.append(worker.block_until_ready_buffers.remote(ary_uuids))
        ray.get(tasks)

    ##### Executable Related Functions #####
    def shard_args_to_bufs(self, shard_indices: Sequence[Sequence[Index]],
                           donated_invars: Sequence[bool],
                           batch_invars: Sequence[bool], num_micro_batches: int,
                           args: Sequence[Any]):
        ret_bufs = []
        total_bytes = 0
        time_start = time.time()

        for arg, indices, donated, is_batch_var in zip(args, shard_indices,
                                                       donated_invars,
                                                       batch_invars):
            tic = time.time()
            slow_path = False

            if is_batch_var:
                if (isinstance(arg, DistributedArray) and
                        arg.skip_shard_args_check is True):
                    assert num_micro_batches == 1
                    ret_bufs.append([arg.remote_ref])
                else:
                    slow_path = True
                    if not isinstance(arg, ShapedArray):
                        arg = np.asarray(arg)
                    refs = _shard_array(arg, self, indices, num_micro_batches)
                    ret_bufs.append(refs)
            else:
                if (isinstance(arg, DistributedArray) and
                        arg.device_mesh == self and arg.indices == indices):
                    # Fast path for DistributedArray
                    ret_bufs.append(arg.remote_ref)
                elif isinstance(arg, ReplicatedDistributedArray):
                    replica = arg.get_replica_on_mesh(self)
                    assert replica.indices == indices
                    ret_bufs.append(replica.remote_ref)
                else:  # Slow path
                    slow_path = True
                    if type(arg) not in [ShapedArray, ShapeDtypeStruct]:
                        arg = xla.canonicalize_dtype(arg)
                    ref = shard_arg_handlers[type(arg)](arg, self, indices)[0]
                    ret_bufs.append(ref)
                    if donated and hasattr(arg, "delete"):
                        # shard_arg_handler always creates new buffers,
                        # so we can delete the old buffers
                        arg.delete()

            if False and slow_path:  # pylint: disable=condition-evals-to-constant
                # Print debug info
                size = np.prod(arg.shape) * arg.dtype.itemsize
                bandwidth = size / (time.time() - tic)
                total_bytes += size
                print("Slow path. "
                      f"shape: {arg.shape}, "
                      f"bandwidth: {bandwidth/1024**2:.2f} MB/s "
                      f"total_bytes: {total_bytes/1024**2:.2f} MB "
                      f"total_time: {time.time() - time_start:.2f}")

        return ret_bufs

    def shard_args_to_arrays(self, avals: Sequence[ShapedArray],
                             shard_indices: Sequence[Sequence[Index]],
                             sharding_specs: Sequence[ShardingSpec],
                             args: Sequence[np.array]):
        arrays = []
        for i in range(len(avals)):
            remote_ref = _shard_array(args[i], self, shard_indices[i])[0]
            arrays.append(
                DistributedArray(self, avals[i], sharding_specs[i], remote_ref,
                                 shard_indices[i]))
        return arrays

    def get_outputs_handler(self, avals: Sequence[ShapedArray],
                            sharding_specs: Sequence[ShardingSpec]):
        indices = [
            pxla.spec_to_indices(aval.shape, spec)
            for aval, spec in zip(avals, sharding_specs)
        ]

        def outs_handler(refs):
            ret = []
            for i, aval in enumerate(avals):
                dis_array = DistributedArray(device_mesh=self,
                                             aval=aval,
                                             sharding_spec=sharding_specs[i],
                                             remote_ref=refs[i],
                                             indices=indices[i])
                ret.append(dis_array)
            return ret

        return outs_handler

    def delete_remote_executable(self, exec_uuid: int):
        """Delete remote worker executables of a driver executable."""
        if not self.workers or not ray or not ray_worker or not np.array:
            return

        try:
            for w in self.workers:
                w.delete_executable.remote(exec_uuid)
        except AttributeError:
            pass

    def set_runtime_random_seed(self, seed: int):
        for w in self.workers:
            w.set_runtime_random_seed.remote(seed)

    ##### Profiling and Debugging Related Functions #####
    def profile_hlo_ops(self,
                        op_infos: Sequence[Tuple],
                        cache_filename: str,
                        single_timeout: Optional[float] = None,
                        batch_timeout: Optional[float] = None):
        tasks = []
        for w in self.workers:
            tasks.append(
                w.profile_hlo_ops.remote(op_infos, cache_filename,
                                         single_timeout))
        return ray.get(tasks, timeout=batch_timeout)[0]

    def get_remote_timer(self, timer_name: str):
        return ray.get(self.workers[0].get_timer.remote(timer_name))

    def reset_remote_timer(self, timer_name: str):
        for worker in self.workers:
            ray.get(worker.reset_timer.remote(timer_name))

    def get_remote_tracer(self):
        return ray.get(self.workers[0].get_tracer.remote())

    def get_memory_allocated(self):
        return max(
            ray.get([w.get_memory_allocated.remote() for w in self.workers]))

    def get_max_memory_allocated(self):
        return max(
            ray.get([w.get_max_memory_allocated.remote() for w in self.workers
                    ]))

    def get_available_memory(self):
        return min(
            ray.get([w.get_available_memory.remote() for w in self.workers]))

    def reset_memory_stats(self):
        for worker in self.workers:
            ray.get(worker.reset_memory_stats.remote())

    ##### Other Functions #####
    def sync_workers(self, sync_all_devices=False):
        ray.get([w.sync.remote(sync_all_devices) for w in self.workers])

    def sync_move_workers(self):
        ray.get([w.sync_move_worker.remote() for w in self.workers])

    def shutdown(self, forced=False):
        self.operation_executables.clear()
        if not self.launched:
            return
        if not forced:
            ray.get([w.shutdown.remote() for w in self.workers])
        for worker in self.workers:
            ray.kill(worker)
        self.workers = None
        # shutdown grpc server
        if self.service_server:
            self.service_server.shutdown()
            self.service_server = None
        self.launched = False


########################################
# Distributed Array and Buffers
########################################
class RemoteArrayRef:
    """
    A reference to all device buffers of a logical array.

    In Alpa, each pipeshard stage runs in SPMD(single program, multiple device).
    Hence, buffers of the same logical array are allocated, used and freed
    together, and thus we use one reference for all these buffers.
    """

    def __init__(self, device_mesh: PhysicalDeviceMesh, uuid: int = None):
        self.device_mesh = device_mesh
        self.uuid = (uuid if uuid is not None else next_array_uuids()[0])
        self.is_deleted_on_workers = False

    def set_deleted_on_workers(self):
        """
        Set the array as deleted on workers.
        For some arrays (e.g., donated tensor), if we know the workers has
        already deleted them, then we do not need to do the remote call
        "delete_remote_buffers" again.
        """
        self.is_deleted_on_workers = True

    def __repr__(self):
        return (f"RemoteBufferRef(uuid = {self.uuid}, "
                f"loc = Mesh ({self.device_mesh.mesh_id}))")

    def __del__(self):
        if not self.is_deleted_on_workers:
            self.device_mesh.delete_remote_buffers((self,))


# The global buffer counter
remote_buffer_counter = 0


def next_array_uuids(number=1):
    """Return the next uuid of a remote buffer."""
    global remote_buffer_counter
    ret = np.arange(remote_buffer_counter, remote_buffer_counter + number)
    remote_buffer_counter = (remote_buffer_counter + number) % (1 << 60)
    return ret


def create_remote_array_refs(device_mesh, number=1):
    """Create a list of remote array refs."""
    ary_uuids = next_array_uuids(number)
    ary_refs = [RemoteArrayRef(device_mesh, uuid) for uuid in ary_uuids]
    return ary_refs, ary_uuids


class DistributedArray:
    """A distributed array on a PhysicalDeviceMesh.

    End users can interact with this array as if they are working with
    a normal numpy array.

    Internally, it stores a pointer to all remote buffers.
    The buffers are stored distributedly on remote workers' device memory.
    When users require the value of the array. These buffers will be gathered
    to the driver.
    """

    def __init__(self,
                 device_mesh: PhysicalDeviceMesh,
                 aval: ShapedArray,
                 sharding_spec: ShardingSpec,
                 remote_ref: RemoteArrayRef,
                 indices: Optional[Sequence[Index]] = None):
        self.device_mesh = device_mesh
        self.aval = aval
        self.sharding_spec = sharding_spec
        self.remote_ref = remote_ref

        if indices is None:
            indices = pxla.spec_to_indices(self.aval.shape, self.sharding_spec)
        self.indices = indices

        self.shape = self.aval.shape
        self.dtype = self.aval.dtype
        self._npy_value = None
        self._fetched_np_buffers = None
        self._fetched_np_buffers_ref = None
        self.skip_shard_args_check = False

    @property
    def size(self):
        return np.prod(self.shape)

    def prefetch(self):
        # TODO (yinmin): Move this function out of DistributedArray
        #  and batch different requests. Also need to add another
        #  function to `ray.wait` for the remote references.
        self._fetched_np_buffers_ref = self.device_mesh.get_remote_buffers(
            (self.remote_ref,), (self.one_replica_host_local_ids,), False,
            True)[0]

    def block_until_ready(self):
        """Block until all remote buffers of this array are ready."""
        self.device_mesh.block_until_ready_remote_buffers([self.remote_ref])

    def delete(self):
        self.remote_ref = None
        self._npy_value = None

    def flush(self):
        self._npy_value = None

    async def to_np_async(self):
        if self._npy_value is None:
            npy_value = np.empty(self.aval.shape, self.aval.dtype)
            if not self._fetched_np_buffers:
                if not self._fetched_np_buffers_ref:
                    self.prefetch()
                fetched_np_buffers = await asyncio.gather(
                    *self._fetched_np_buffers_ref)
            else:
                fetched_np_buffers = self._fetched_np_buffers
            for ct, i in enumerate(self.one_replica_buffer_ids):
                npy_value[self.indices[i]] = fetched_np_buffers[ct]
            self._npy_value = npy_value
        return self._npy_value

    ##### distributed save/load #####
    def save(self, ckpt_dir: str, local_cache_dir: Union[str, None] = None):
        """
            Save one replica of the array to `ckpt_dir` distributedly.

            Args:
                ckpt_dir: The directory where all the shards of
                this array will be saved.
                local_cache_dir: If not None, `ckpt_dir` should be a shared
                filesystem path, and this function will return as soon as the
                shards have been saved to this local directory.
                DaemonMoveWorkers will move these shards into `ckpt_dir`
                in the background.

        """
        one_replica_indices = [
            self.indices[i] for i in self.one_replica_buffer_ids
        ]
        device_ids_per_host = {}
        indices_per_host = {}
        for buf_id, indice in zip(self.one_replica_host_local_ids,
                                  one_replica_indices):
            host_id, device_id = buf_id
            if indices_per_host.get(host_id) is None:
                indices_per_host[host_id] = [indice]
                device_ids_per_host[host_id] = [device_id]
            else:
                indices_per_host[host_id].append(indice)
                device_ids_per_host[host_id].append(device_id)
        for host_id, indices in indices_per_host.items():
            if len(indices) > 0:
                self.device_mesh.workers[host_id].save_array.remote(
                    ckpt_dir, local_cache_dir, self.remote_ref.uuid,
                    np.array(device_ids_per_host[host_id]), indices, self.shape)

    @classmethod
    def load(cls, path: str, aval: ShapedArray, device_mesh: PhysicalDeviceMesh,
             sharding_spec: ShardingSpec):
        """
            Load the data from `path` distributedly with `aval` and
            return a new DistributedArray
        """
        # pylint: disable=import-outside-toplevel
        ary_ref = RemoteArrayRef(device_mesh)
        indices = pxla.spec_to_indices(aval.shape, sharding_spec)

        indices_per_host = {}
        device_ids_per_host = {}
        for buf_idx, indice in enumerate(indices):
            host_id, device_id = divmod(buf_idx,
                                        device_mesh.num_devices_per_host)
            if indices_per_host.get(host_id) is None:
                indices_per_host[host_id] = [indice]
                device_ids_per_host[host_id] = [device_id]
            else:
                indices_per_host[host_id].append(indice)
                device_ids_per_host[host_id].append(device_id)
        for host_id, worker in enumerate(device_mesh.workers):
            worker.load_array.remote(path, ary_ref.uuid,
                                     device_ids_per_host[host_id],
                                     indices_per_host[host_id])
        return DistributedArray(device_mesh, aval, sharding_spec, ary_ref,
                                indices)

    @property
    def one_replica_buffer_ids(self):
        """Indices of buffers containing one complete copy of the array data."""
        return self.device_mesh._compute_one_replica_ids(
            self.indices, self.aval.shape, self.sharding_spec)[0]

    @property
    def one_replica_host_local_ids(self):
        return self.device_mesh._compute_one_replica_ids(
            self.indices, self.aval.shape, self.sharding_spec)[1]

    @property
    def _value(self):
        if self._npy_value is None:
            npy_value = np.empty(self.aval.shape, self.aval.dtype)
            if not self._fetched_np_buffers:
                if not self._fetched_np_buffers_ref:
                    fetched_np_buffers = self.device_mesh.get_remote_buffers(
                        (self.remote_ref,),
                        (self.one_replica_host_local_ids,))[0]
                else:
                    fetched_np_buffers = ray.get(self._fetched_np_buffers_ref)
            else:
                fetched_np_buffers = self._fetched_np_buffers
            for ct, i in enumerate(self.one_replica_buffer_ids):
                npy_value[self.indices[i]] = fetched_np_buffers[ct]
            self._npy_value = npy_value
        return self._npy_value

    def __array__(self, dtype=None, context=None):
        # pylint: disable=unused-argument
        return np.asarray(self._value, dtype=dtype)

    def __float__(self):
        return self._value.__float__()

    # TODO(lmzheng): copy more functions from DeviceArray
    #   (jax/_src/device_array.py)

    def __str__(self):
        return (f"DistributedArray(sharding_spec={self.sharding_spec}, "
                f"value={self._value})")

    def __del__(self):
        self.delete()


core.pytype_aval_mappings[DistributedArray] = attrgetter("aval")
xla.pytype_aval_mappings[DistributedArray] = attrgetter("aval")
xla.canonicalize_dtype_handlers[DistributedArray] = lambda x: x


class ReplicatedDistributedArray:
    """A distributed array that is replicated on multiple meshes.

    These class is used for arrays that need to be replicated on
    multiple physical meshes (e.g., optimizer's step).
    """

    def __init__(self, device_meshes: Sequence[PhysicalDeviceMesh],
                 arrays: Sequence[DistributedArray]):
        self._mesh_array_map = {}
        self._array_mesh_map = {}
        for mesh, array in zip(device_meshes, arrays):
            self._mesh_array_map[mesh] = array
            self._array_mesh_map[array] = mesh
        self.aval = self.replica.aval

    def is_replicated_on_mesh(self, mesh: PhysicalDeviceMesh):
        """Whether this distributed array is on a given mesh."""
        if mesh in self._mesh_array_map:
            return True
        return False

    def get_replica_on_mesh(self, mesh: PhysicalDeviceMesh):
        if not self.is_replicated_on_mesh(mesh):
            raise RuntimeError("No replica found on this mesh.")
        return self._mesh_array_map[mesh]

    def add_replica(self, mesh: PhysicalDeviceMesh, array: DistributedArray):
        assert isinstance(array, DistributedArray)
        assert isinstance(mesh, PhysicalDeviceMesh)
        if array in self._array_mesh_map:
            raise RuntimeError("Replica exists.")
        if mesh in self._mesh_array_map:
            raise RuntimeError("Mesh exists.")
        self._mesh_array_map.update({mesh: array})
        self._array_mesh_map.update({array: mesh})

    @property
    def replica(self):
        return list(self._mesh_array_map.values())[0]

    @property
    def _value(self):
        return self.replica._value

    def __array__(self, dtype=None, context=None):
        # pylint: disable=unused-argument
        return np.asarray(self._value, dtype=dtype)

    def __str__(self):
        return str(self._value)


core.pytype_aval_mappings[ReplicatedDistributedArray] = attrgetter("aval")
xla.pytype_aval_mappings[ReplicatedDistributedArray] = attrgetter("aval")
xla.canonicalize_dtype_handlers[ReplicatedDistributedArray] = lambda x: x


def prefetch(dis_arrays: Sequence[Union[ShardedDeviceArray, DistributedArray,
                                        ReplicatedDistributedArray]]):
    """Prefetch a pytree of DistributedArray in a batch.

    If you want to get a lot of DistributedArrays from remote workers,
    call this batched prefetch can make the later access faster.
    """
    group_by_mesh = defaultdict(list)
    for array in tree_leaves(dis_arrays):
        if isinstance(array, ShardedDeviceArray):
            array.copy_to_host_async()
        elif isinstance(array, DistributedArray):
            group_by_mesh[array.device_mesh].append(array)
        elif isinstance(array, ReplicatedDistributedArray):
            array = array.replica
            group_by_mesh[array.device_mesh].append(array)
        else:
            raise ValueError(f"Unhandled array type: {array}")

    for device_mesh, arrays in group_by_mesh.items():
        buf_refs = []
        host_local_ids = []
        for array in arrays:
            buf_refs.append(array.remote_ref)
            host_local_ids.append(array.one_replica_host_local_ids)

        np_arrays = device_mesh.get_remote_buffers(buf_refs,
                                                   host_local_ids,
                                                   batching=True)

        for array, np_value in zip(arrays, np_arrays):
            array._fetched_np_buffers = np_value  # pylint: disable=protected-access


########################################
##### Physical Mesh Group #####
########################################
class VirtualPhysicalMesh:
    """
    A virtual physical mesh used for pipeline parallel compilation.

    VirtualPhysicalMesh is used during compile time. We don't allocate actual
    workers for it. When compilation is finished, we instantiated it as a
    PhysicalDeviceMesh and launch workers.

    A VirtualPhysicalMesh can also be sliced into multiple VirtualPhysicalMesh.
    After slicing, each sliced VirtualPhysicalMesh can be instantiated as a
    PhysicalDeviceMesh. These sliced PhysicalDeviceMesh together can form a
    PhysicalDeviceMeshGroup for pipeline parallelism.
    """

    def __init__(self,
                 host_ids: Sequence[int],
                 host_info: Sequence[dict],
                 num_devices_per_host,
                 gpu_info_per_node: dict,
                 #is_gpulet_mesh: bool,
                 parent: "VirtualPhysicalMesh" = None,
                 devices: Sequence[Sequence[int]] = None):
        # host_ids are the indices of hosts in the global DeviceCluster
        self.host_ids = host_ids
        self.host_info = host_info
        self.num_devices_per_host = num_devices_per_host
        self.parent = parent

        self.nodeid_to_hostid = {}
        for host_id, host_info in zip(self.host_ids, self.host_info):
            self.nodeid_to_hostid[host_info['NodeID']] = host_id

        self.gpu_info_per_node = gpu_info_per_node
        self.num_gpus = 0
        for info in self.gpu_info_per_node.values():
            self.num_gpus += len(info)
        #self.is_gpulet_mesh = is_gpulet_mesh

        self.launched_physical_mesh = None
        self.launched_physical_mesh_group = None

        if devices is not None:
            if len(devices) != len(host_ids):
                raise RuntimeError(
                    "Please specify the gpu IDs used on each host.")
            if not all(len(ids) == num_devices_per_host for ids in devices):
                raise RuntimeError(
                    "Device IDs specified for each host does not align "
                    "with `num_devices_per_host`.")
        else:
            devices = [list(range(num_devices_per_host)) for _ in host_ids]

        self.devices = devices
        # Depending on gpu_ids, generate device strs and ask Ray to allocate.
        self.device_strs = []
        for i in range(self.num_hosts):
            ip = self.host_info[i]["NodeManagerAddress"]
            self.device_strs.extend(
                [device_id_to_str(ip, j) for j in devices[i]])

    @property
    def shape(self):
        return (len(self.host_ids), self.num_devices_per_host)

    @property
    def num_devices(self):
        """Return the total number of GPUs on this mesh."""
        return len(self.host_ids) * self.num_devices_per_host

    @property
    def num_hosts(self):
        """Return the number of hosts in the mesh."""
        return len(self.host_ids)

    def slice_1d(self, dim: int, indices: Sequence[int]):
        """
        Slice a mesh given the slicing config.

        Args:
            dim: which dimension to slice from, 0 is host or 1 is the gpu
            indices: indices to include along this dimension.

        Returns:
            mesh (PhysicalDeviceMesh)
        """
        if dim == 0:
            # slicing along the host dimension
            host_ids = [self.host_ids[x] for x in indices]
            host_info = [self.host_info[x] for x in host_ids]
            return VirtualPhysicalMesh(
                host_ids=host_ids,
                host_info=host_info,
                num_devices_per_host=self.num_devices_per_host,
                parent=self)
        else:
            # slicing along the device dimension

            # Check the validity of device_indices
            for i in range(len(indices)):
                for x in indices[i]:
                    assert x in self.devices[i]

            return VirtualPhysicalMesh(host_ids=self.host_ids,
                                       host_info=self.host_info,
                                       num_devices_per_host=len(indices[0]),
                                       parent=self,
                                       devices=indices)

    def print_gpus_info(self):
        for key in self.gpu_info_per_node.keys():
            gpu_info = self.gpu_info_per_node[key]
            print(f"node {key} has {len(gpu_info)} GPUs")
            for gpu in gpu_info:
                gpu.print_info()

    def print(self):
        print()
        print("-*-*-*-*-VirtualPhysicalMesh-*-*-*-*-")
        print("host_ids: ", self.host_ids)
        print("host_info: ", self.host_info)
        print("num_devices_per_host: ", self.num_devices_per_host)
        print("devices: ", self.devices)
        print("num_gpus: ", self.num_gpus)
        self.print_gpus_info()
        print("-*-*-*-*-VirtualPhysicalMesh  END-*-*-*-*-")


    def slice_2d(self, host_indices, device_indices):
        host_ids = [self.host_ids[x] for x in host_indices]
        host_info = [self.host_info[x] for x in host_indices]

        gpu_info_per_node = {}
        for host in host_info:
            node_id = host["NodeID"]
            gpu_info_per_node[node_id] = self.gpu_info_per_node[node_id]


        # Check the validity of device_indices
        for i in range(len(device_indices)):
            for x in device_indices[i]:
                assert x in self.devices[i]

        return VirtualPhysicalMesh(host_ids=host_ids,
                                   host_info=host_info,
                                   num_devices_per_host=len(device_indices[0]),
                                   gpu_info_per_node=gpu_info_per_node,
                                   parent=self,
                                   devices=device_indices)

    def slice_2d_from_gpulet_mesh(self, gpu_infos: Sequence[PhysicalGPU]):
        host_ids = []
        host_info = []
        devices = []
        gpu_info_per_node = {}

        """self.gpu_id = PhysicalGPU.gpu_id
        PhysicalGPU.gpu_id += 1

        self.gpu_name = gpu_name
        self.uuid = uuid
        self.compute_capacity = ComputeCapacityTable[self.gpu_name]
        self.memory = float(float(memory) / 1024)
        self.node_id = node_id
        self.gpulets = []
        self.num_gpulets = 0"""
        prev_host_id = -1
        prev_node_id = None
        tmp_devices = []
        tmp_gpu_infos = []
        for gpu_info in gpu_infos:
            host_id = self.nodeid_to_hostid[gpu_info.node_id]
            if host_id != prev_host_id and prev_host_id != -1:
                devices.append(tmp_devices)
                tmp_devices = []
                gpu_info_per_node[prev_node_id] = tmp_gpu_infos
                tmp_gpu_infos = []
                host_ids.append(prev_host_id)
            
            host_info.append(self.host_info[host_id])
            tmp_gpu_infos.append(gpu_info)
            tmp_devices.append(gpu_info.gpu_id)
            prev_host_id = host_id
            prev_node_id = gpu_info.node_id
        
        host_ids.append(prev_host_id)
        devices.append(tmp_devices)
        gpu_info_per_node[prev_node_id] = tmp_gpu_infos
        num_devices_per_host = max([len(x) for x in devices])

            
        return VirtualPhysicalMesh(host_ids=host_ids,
                                   host_info=host_info,
                                   num_devices_per_host=num_devices_per_host,
                                   gpu_info_per_node=gpu_info_per_node,
                                   parent=self,
                                   devices=devices)



    def slice_profiling_submeshes(self, submesh_num_hosts,
                                  submesh_num_devices_per_host):
        num_hosts = len(self.host_ids)
        num_devices_per_host = self.num_devices_per_host
        num_host_submeshes = num_hosts // submesh_num_hosts
        num_device_submeshes = (num_devices_per_host //
                                submesh_num_devices_per_host)
        all_submeshes = []
        for i in range(num_host_submeshes):
            for j in range(num_device_submeshes):
                host_indices = range(i * submesh_num_hosts,
                                     (i + 1) * submesh_num_hosts)
                device_indices = [
                    range(j * submesh_num_devices_per_host,
                          (j + 1) * submesh_num_devices_per_host)
                    for _ in host_indices
                ]
                all_submeshes.append(self.slice_2d(host_indices,
                                                   device_indices))
        return all_submeshes

    def get_logical_mesh(self,
                         mesh_shape: Optional[Sequence[int]] = None,
                         mesh_alpha: Optional[float] = None,
                         mesh_beta: Optional[float] = None):
        """
        Return a logical mesh and parameters of the alpha-beta communication
        cost model. The logical view is used for auto-sharding.
        """
        if mesh_shape is None:
            mesh_shape = (self.num_hosts, self.num_devices_per_host)

        id_mesh = np.arange(self.num_devices).reshape(mesh_shape)
        mesh_alpha = mesh_alpha or (1, 1)
        mesh_beta = mesh_beta or (1, 0.1)
        return LogicalDeviceMesh(None, id_mesh, mesh_alpha, mesh_beta)

    def get_physical_mesh(self, mesh_id: int = 0):
        """Launch a physical mesh (which will request resources from Ray)."""
        assert self.launched_physical_mesh is None, \
            "Physical mesh can only be launched once."
        
        #logger.info(f"Launching physical mesh (mesh_id){mesh_id} on (host_ids){self.host_ids}"\
        #            f" (devices){self.devices}")
        logger.info(f"num_devices_per_host: {self.num_devices_per_host}")
        self.launched_physical_mesh = DistributedPhysicalDeviceMesh(
            host_ids=self.host_ids,
            host_info=self.host_info,
            num_devices_per_host=self.num_devices_per_host,
            parent=self,
            devices=self.devices,
            mesh_id=mesh_id)
        return self.launched_physical_mesh

    def get_physical_mesh_group(self, sliced_virtual_meshes):
        """Launch a physical mesh group (which will request resources from
        Ray)."""
        assert self.launched_physical_mesh_group is None, \
            "Physical mesh group can only be launched once."

        # Launch physical meshes in parallel
        physical_meshes = [None] * len(sliced_virtual_meshes)

        def launch_func(i):
            physical_meshes[i] = sliced_virtual_meshes[i].get_physical_mesh(i)

        threads = []
        for i in range(len(sliced_virtual_meshes)):
            t = threading.Thread(target=launch_func, args=(i,))
            t.start()
            threads.append(t)
        for i in range(len(sliced_virtual_meshes)):
            threads[i].join()

        self.launched_physical_mesh_group = (PhysicalDeviceMeshGroup(
            physical_meshes, self))
        return self.launched_physical_mesh_group


    def get_gpulet_mesh(self):
        #assert self.is_gpulet_mesh, "This mesh is not a GPUletMesh."
        gpu_ids = []
        gpu_infos = []
        for info in self.gpu_info_per_node.values():
            for gpu in info:
                gpu_ids.append(gpu.gpu_id)
                gpu_infos.append(gpu)

        #gpu_infos = [gpuinfo for gpuinfo in self.gpu_info_per_node.values()]
        #print(self.gpu_info_per_node.values())
        #gpu_infos = list(self.gpu_info_per_node.values())

        return GPUletMesh(
            gpu_ids=gpu_ids,
            gpu_infos=gpu_infos,
            belong_to=self,
            parent=None, # 表明这个GPUletMesh是由哪个GPUletMesh切分而来，
                         # 对于由VirtualPhyscialMesh转化的GPUletMesh而言，parent应该为None
        )



class GPUletMesh():
    """A mesh composed of multiple GPUlets.
    对应VirtualPhysicalMesh. 如果启动了GPUlets选项，那么在编译过程中用GPUletMesh替换
    VirtualPhysicalMesh.

    需要格外注意一些函数的重载
    """


    def __init__(self,
                 gpu_ids: Sequence[int],
                 #gpulets
                 gpu_infos: Sequence[PhysicalGPU],
                 belong_to: "VirtualPhysicalMesh" = None,   #对应于哪个VirtualPhysicalMesh
                 parent: "GPUletMesh" = None,   #由哪个GPUletMesh切分而来
                 gpulets: Sequence[Sequence[GPUlet]] = None    #第一个维度表示不同的gpu，第二个维度表示同一个gpu内的gpulet的id
                ):
       
        self.gpu_ids = gpu_ids
        self.gpu_infos = gpu_infos
        self.num_gpu = len(gpu_ids) #mesh中物理gpu的数量，对应于VirtualPhysicalMesh中的host
        self.belong_to = belong_to
        self.parent = parent


        if gpulets is None:
            gpulets = []
            for gpu in self.gpu_infos:
                gpulets.append(gpu.gpulets)

        self.gpulets = gpulets
        # 当二维数组内的每个一维数组长度不同时，使用np.flatten不会将其转化为一维
        #self.flatten_gpulets = np.array(gpulets).flatten().tolist()
        self.flatten_gpulets = []
        for gpulet in gpulets:
            self.flatten_gpulets.extend(gpulet)



        self.flatten_gpuids = []
        for gpulet in self.flatten_gpulets:
            self.flatten_gpuids.append(gpulet.belong_to.gpu_id)
        


        self.num_gpulets_per_gpu = [len(x) for x in self.gpulets]
        self.num_gpulets = sum(self.num_gpulets_per_gpu)

        #self.launched_physical_mesh = None
        #self.launched_physical_mesh_group = None

        '''
        if devices is not None:
            if len(devices) != len(host_ids):
                raise RuntimeError(
                    "Please specify the gpu IDs used on each host.")
            if not all(len(ids) == num_devices_per_host for ids in devices):
                raise RuntimeError(
                    "Device IDs specified for each host does not align "
                    "with `num_devices_per_host`.")
        else:
            devices = [list(range(num_devices_per_host)) for _ in host_ids]
            

        self.devices = devices
        # Depending on gpu_ids, generate device strs and ask Ray to allocate.
        self.device_strs = []
        for i in range(self.num_hosts):
            ip = self.host_info[i]["NodeManagerAddress"]
            self.device_strs.extend(
                [device_id_to_str(ip, j) for j in devices[i]])
        '''


    def slice_2d(self, gpu_indices, gpulet_indices):
        raise NotImplementedError("GPUletMesh does not support slice_2d() now.")
        gpu_ids = [self.gpu_ids[x] for x in gpu_indices]
        gpu_infos = [self.gpu_infos[x] for x in gpu_indices]

        # Check the validity of device_indices
        for i in range(len(gpulet_indices)):
            for x in gpulet_indices[i]:
                assert x in self.gpulets[i]

        return GPUletMesh(gpu_ids=gpu_ids,
                        gpu_infos=gpu_infos,
                        parent=self,
                        belong_to=self.belong_to,
                        gpulets=gpulet_indices)


    def slice_1d(self, gpulet_indices: Sequence[int]):
        """
        原本整个GPUletMesh是一个二维结构，但是为了解决可能存在的集群物理拓扑的异构性（即每个节点的GPU数量不同），
        （同时即使每个节点的GPU数量相同，但由于显存的不同，所以每个物理节点的GPUlet数量也会不同）
        目前的解决方案是在生成profiling阶段时，将整个GPUletMesh的二维结构压缩为一维进行切分

        具体而言slice_1d根据类中的flatten_gpulets进行切分，获取构造函数需要的信息
        """
        gpu_ids = []
        gpu_infos = []
        gpulets =  []
        prev_gpu_id = -1
        prev_gpu = None
        tmp_gpulets = []
        for i in gpulet_indices:
            print(f"i = {i}, self.num_gpulets = {self.num_gpulets}")
            assert i < self.num_gpulets, "gpulet index out of range"
            gpulet = self.flatten_gpulets[i]
            if prev_gpu_id != gpulet.belong_to.gpu_id:
                if prev_gpu_id != -1:
                    gpulets.append(tmp_gpulets)
                    tmp_gpulets = []
                    gpu_ids.append(prev_gpu_id)
                    gpu_infos.append(prev_gpu)
                    #prev_gpu_id = gpulet.belong_to.gpu_id
            prev_gpu = gpulet.belong_to
            prev_gpu_id = prev_gpu.gpu_id
            tmp_gpulets.append(gpulet)

        gpulets.append(tmp_gpulets)
        gpu_ids.append(prev_gpu_id)
        gpu_infos.append(prev_gpu)

        return GPUletMesh(gpu_ids=gpu_ids,
                          gpu_infos=gpu_infos,
                          parent=self,
                          belong_to=self.belong_to,
                          gpulets=gpulets)




    def get_logical_gpulet_mesh(self,
                         mesh_shape: Optional[Sequence[int]] = None,
                         mesh_alpha: Optional[float] = None,
                         mesh_beta: Optional[float] = None):
        """
        Return a logical mesh and parameters of the alpha-beta communication
        cost model. The logical view is used for auto-sharding.
        """
        if mesh_shape is None:
            raise NotImplementedError("GPUletMesh does not support get_logical_mesh() when mesh_shape is None.")
            #mesh_shape = (self.num_hosts, self.num_devices_per_host)

        id_mesh = np.arange(self.num_gpulets).reshape(mesh_shape)
        #id_mesh = np.array().reshape(mesh_shape)

        mesh_alpha = mesh_alpha or (1, 1)
        mesh_beta = mesh_beta or (1, 0.1)
        return LogicalDeviceMesh(None, id_mesh, mesh_alpha, mesh_beta)
    

    def slice_profiling_gpulet_submeshes(self, num_gpulets_per_submesh):
        """num_hosts = len(self.host_ids)
        num_devices_per_host = self.num_devices_per_host
        num_host_submeshes = num_hosts // submesh_num_hosts
        num_device_submeshes = (num_devices_per_host //
                                submesh_num_devices_per_host)
        all_submeshes = []
        for i in range(num_host_submeshes):
            for j in range(num_device_submeshes):
                host_indices = range(i * submesh_num_hosts,
                                     (i + 1) * submesh_num_hosts)
                device_indices = [
                    range(j * submesh_num_devices_per_host,
                          (j + 1) * submesh_num_devices_per_host)
                    for _ in host_indices
                ]
                all_submeshes.append(self.slice_2d(host_indices,
                                                   device_indices))"""

        num_gpulet_submeshes = self.num_gpulets // num_gpulets_per_submesh
        all_submeshes = []

        for i in range(num_gpulet_submeshes):
            gpulet_indices = tuple(
                range(i * num_gpulets_per_submesh,
                      (i + 1) * num_gpulets_per_submesh)
            )
            print("gpulet_indices: ", gpulet_indices)
            all_submeshes.append(self.slice_1d(gpulet_indices))

        return all_submeshes


    def back_to_virtual_mesh(self):
        



        virtual_mesh = self.belong_to.slice_2d_from_gpulet_mesh(self.gpu_infos)
        return virtual_mesh


    def print(self):
        print("-*-*-*-*-GPUletMesh-*-*-*-*-")
        print("gpu_ids: ", self.gpu_ids)
        print("gpu_infos: ", self.gpu_infos)
        print("num_gpu: ", self.num_gpu)
        print("gpulets: ", self.gpulets)
        print("-*-*-*-*-GPUletMesh END-*-*-*-*-")




class PhysicalDeviceMeshGroup:
    """A list of physical devices that forms a pipeline."""

    def __init__(self, meshes: Sequence[DistributedPhysicalDeviceMesh],
                 parent: VirtualPhysicalMesh):
        self.meshes = list(meshes)
        self.parent = parent
        self.collective_groups: List[List[Any]] = [
            [None for _ in range(len(self))] for _ in range(len(self))
        ]

    def __getitem__(self, index):
        return self.meshes[index]

    def __len__(self):
        return len(self.meshes)

    def index(self, *args, **kwargs):
        return self.meshes.index(*args, **kwargs)

    def establish_nccl_group(self,
                             src_mesh_id: int,
                             dst_mesh_id: int,
                             instantiate=True):
        """Establish NCCL group between two meshes."""
        # pylint: disable=import-outside-toplevel
        from alpa.pipeline_parallel.cross_mesh_resharding import CollectiveGroup

        assert src_mesh_id < dst_mesh_id
        if self.collective_groups[src_mesh_id][dst_mesh_id] is not None:
            # Already established
            return
        src_mesh = self.meshes[src_mesh_id]
        dst_mesh = self.meshes[dst_mesh_id]
        device_strs = OrderedSet(src_mesh.device_strs + dst_mesh.device_strs)
        cg = CollectiveGroup(device_strs, src_mesh, dst_mesh)
        self.collective_groups[src_mesh_id][dst_mesh_id] = cg
        self.collective_groups[dst_mesh_id][src_mesh_id] = cg
        if instantiate:
            self._instantiate_nccl_group(cg)

    def instantiate_nccl_group(self, src_mesh_id: int, dst_mesh_id: int):
        cg = self.collective_groups[src_mesh_id][dst_mesh_id]
        self._instantiate_nccl_group(cg)

    def shard_args_to_arrays(self, placement_specs: PlacementSpec,
                             args: Sequence[Any]):
        rets = []

        for info, arg in zip(placement_specs, args):
            aval = info.aval
            if len(info.mesh_ids) == 1:
                mesh = self.meshes[info.mesh_ids[0]]
                spec = info.sharding_specs[0]
                indices = pxla.spec_to_indices(aval.shape, spec)
                rets.append(
                    mesh.shard_args_to_arrays((aval,), (indices,), (spec,),
                                              (arg,))[0])
            else:
                meshes, arrays = [], []
                for mesh_id, spec in zip(info.mesh_ids, info.sharding_specs):
                    mesh = self.meshes[mesh_id]
                    meshes.append(mesh)
                    indices = pxla.spec_to_indices(aval.shape, spec)
                    arrays.append(
                        mesh.shard_args_to_arrays((aval,), (indices,), (spec,),
                                                  (arg,))[0])
                rets.append(ReplicatedDistributedArray(meshes, arrays))

        return rets

    def set_runtime_random_seed(self, seed: int):
        for m in self.meshes:
            m.set_runtime_random_seed(seed)

    def sync_workers(self):
        """Sync device activities on all workers."""
        all_workers = [w for mesh in self.meshes for w in mesh.workers]
        ray.get([w.sync.remote() for w in all_workers])

    def sync_move_workers(self):
        """Sync moveworkers on all meshes."""
        for mesh in self.meshes:
            mesh.sync_move_workers()

    def get_memory_allocated(self):
        """Get the current size of allocated memory."""
        calls = []
        for mesh in self.meshes:
            for worker in mesh.workers:
                calls.append(worker.get_memory_allocated.remote())
        return max(ray.get(calls))

    def get_max_memory_allocated(self):
        """Get the maximal size of memory allocated so far."""
        calls = []
        for mesh in self.meshes:
            for worker in mesh.workers:
                calls.append(worker.get_max_memory_allocated.remote())
        return max(ray.get(calls))

    def get_max_memory_allocated_per_mesh(self):
        """Get the maximal size of memory allocated for each mesh so far."""
        return [mesh.get_max_memory_allocated() for mesh in self.meshes]

    def reset_memory_stats(self):
        for mesh in self.meshes:
            mesh.reset_memory_stats()

    def destroy_collective_groups(self):
        for i in range(len(self)):
            for j in range(len(self)):
                if i < j and self.collective_groups[i][j] is not None:
                    self.collective_groups[i][j].destroy()

    def shutdown(self):
        self.destroy_collective_groups()
        for mesh in self.meshes:
            mesh.shutdown()

    def exception_shutdown(self):
        """In this shutdown, some actors might have died."""
        # recycle collective group info
        for i in range(len(self)):
            for j in range(len(self)):
                if i < j and self.collective_groups[i][j]:
                    group_name = self.collective_groups[i][j].group_name
                    # TODO(Hao): move this part of recycling to
                    #   ray.util.collective instead of here.
                    name = "info_" + group_name
                    try:
                        store = ray.get_actor(name)
                        ray.kill(store)
                    except ValueError:
                        pass
        # TODO(Hao): recycle the NCCLUniqueID named actor. Their name is MD5
        #  hashed. each of them will take 1 CPU.
        # recycle info actors
        for mesh in self.meshes:
            mesh.shutdown(forced=True)

    @staticmethod
    def _instantiate_nccl_group(cg):
        if global_config.eagerly_create_communicators:
            cg.instantiate_now()
        else:
            cg.instantiate()


########################################
# Device Cluster
########################################
class DeviceCluster:
    """
    A ray cluster with GPU devices.

    This is the top interface for alpa to interact with ray cluster's resources.
    """

    def __init__(self,
                 num_nodes: int = None,
                 num_devices_per_node: int = None,
                 namespace: Optional[str] = None):
        # pylint: disable=import-outside-toplevel
        ray_global_node = ray_worker._global_node
        try:
            self.head_info = ray_global_node.address_info
        except AttributeError as ae:
            raise RuntimeError(
                "Cannot access ray global node. Did you call ray.init?") \
                from ae

        # Gather host ids
        all_host_info = []
        all_host_ips = []


        #整个集群内的GPU信息
        # key: ray.node中的NodeID，value: GPU信息  (name, memory, uuid, capacity)（）
        # 这里的capacity暂时不用，直接查表 e.g. ComputeCapacityTable[name] = capacity
        self.gpu_info_per_node = {}
        
        # devices为二维数组，横向代表不同节点，纵向代表不同设备，每个设备用PhysicalGPU的id表示
        self.devices = []


        for node in ray.nodes():
            if global_config.enable_gpulets:
                gpus = []
                devices = []
                print(f"GPUlets enabled, getting GPU info from node: {node['NodeManagerAddress']}")
                # get the GPU information from each node
                actor = GPUInfoActor.options(num_cpus=1, resources={f"node:{node['NodeManagerAddress']}": 0.01}).remote()
                node_gpu_info = ray.get(actor.get_gpu_info.remote())
                for gpu_name, memory, uuid in node_gpu_info:
                    gpu = PhysicalGPU(gpu_name, uuid, memory, node['NodeID'])
                    gpus.append(gpu)
                    devices.append(gpu.gpu_id)
                ray.kill(actor)
                for gpu in gpus:
                    gpu.set_gpulets()
                self.gpu_info_per_node[node['NodeID']]=gpus
                self.devices.append(devices)

            for key in node["Resources"]:
                if (is_ray_node_resource(key) and
                        global_config.ray_accelerator_name
                        in node["Resources"]):
                    all_host_info.append(node)
                    all_host_ips.append(key.split("node:")[-1])

        # Gather device info
        all_host_num_devices = []
        for host_info in all_host_info:
            number = host_info["Resources"][global_config.ray_accelerator_name]
            assert number.is_integer()
            all_host_num_devices.append(int(number))

        print()
        print(f"all_host_info: {all_host_info}")
        #print(f"all_host_ips: {all_host_ips}")
        print(f"all_host_num_devices: {all_host_num_devices}")

        # adjust the resource allocations
        # if `num_nodes` is set, use it.
        # otherwise, use the number of nodes in cluster
        if num_nodes:
            num_hosts = min(num_nodes, len(all_host_info))
        else:
            num_hosts = len(all_host_info)

        # if `devices_per_node` is set, use it.
        if num_devices_per_node:
            # verify that the number of devices per node is valid
            num_valid = sum(num_device >= num_devices_per_node
                            for num_device in all_host_num_devices)
            if num_valid < num_nodes:
                raise RuntimeError("The number of devices per node is invalid. "
                                   f"There are only {num_valid} valid nodes.")
            # NOTE: for simplicity, we assume `num_devices_per_node` are equal.
            self.host_num_devices = [num_devices_per_node] * num_hosts
        else:
            self.host_num_devices = all_host_num_devices

        # Create placement group
        self.namespace = namespace
        if namespace:
            pg_name = namespace + "_pg"
            try:
                pg = ray.util.get_placement_group(pg_name)
            except ValueError:
                pg = None
        else:
            pg_name = pg = None

        if pg:
            self.placement_group = pg
        else:
            self.placement_group = create_placement_group(
                num_hosts, self.host_num_devices, pg_name)

        # Update the Device Cluster info from placement group
        if num_devices_per_node or num_nodes:
            # map: host ip to host info
            host_ip2info = dict(zip(all_host_ips, all_host_info))

            # get bundle's ip address
            ips = get_bundle2ip(self.placement_group)
            bundle_specs = self.placement_group.bundle_specs

            # filter out the bundle index with device (GPUs)
            device_bundle_idx_list = [
                i for i, bundle_spec in enumerate(bundle_specs)
                if bundle_spec.get("GPU", 0) > 0
            ]

            # filter nodes according to the placement group
            self.host_info = [host_ip2info[ip] for ip in ips]
            self.host_ips = [
                ips[bundle_idx] for bundle_idx in device_bundle_idx_list
            ]
        else:
            self.host_info = all_host_info
            self.host_ips = all_host_ips

    def delete_placement_group(self):
        """remove the placement group for the current device cluster."""
        remove_placement_group(self.placement_group)
        self.placement_group = None

    @property
    def num_cpus(self):
        return sum(
            map(lambda info: int(info["Resources"]["CPU"]), self.host_info))

    @property
    def num_devices(self):
        return sum(self.host_num_devices)

    @property
    def num_hosts(self):
        return len(self.host_info)
    

    def print_gpus_info(self):
        for key in self.gpu_info_per_node.keys():
            gpu_info = self.gpu_info_per_node[key]
            print(f"node {key} has {len(gpu_info)} GPUs")
            for gpu in gpu_info:
                gpu.print_info()

    def get_physical_mesh(self,
                          host_ids: Sequence[int] = None,
                          num_devices_per_host: int = None):
        """
        Slice a subset of hosts and devices to form a physical device mesh.

        Args:
            host_ids: The index of host nodes.
                "None" means using all hosts
            num_devices_per_host: The number of devices per host.
                "None" means using all devices

        Return:
            A physical multi-host device mesh
        """
        host_ids = host_ids or np.arange(len(self.host_info))
        host_info = [self.host_info[x] for x in host_ids]

        num_devices_per_host = num_devices_per_host or self.host_num_devices[
            host_ids[0]]
        for host_id in host_ids:
            assert self.host_num_devices[host_id] >= num_devices_per_host

        return DistributedPhysicalDeviceMesh(
            host_ids=host_ids,
            host_info=host_info,
            num_devices_per_host=num_devices_per_host,
            parent=self,
            namespace=self.namespace)

    def get_virtual_physical_mesh(self,
                                  host_ids: Sequence[int] = None,
                                  num_devices_per_host: int = None):
        """
        Slice a subset of hosts and devices to form a virtual physical mesh.

        The only difference between a virtual and a physical mesh is that a
        virtual mesh does not request cluster resources.
        """
        host_ids = host_ids or np.arange(len(self.host_info))
        host_info = [self.host_info[x] for x in host_ids]

        num_devices_per_host = num_devices_per_host or self.host_num_devices[
            host_ids[0]]
        for host_id in host_ids:
            assert self.host_num_devices[host_id] >= num_devices_per_host

        gpu_info_per_node = {}
        for host in host_info:
            node_id = host['NodeID']
            gpu_info_per_node[node_id]=(self.gpu_info_per_node[node_id])


        if global_config.enable_gpulets:
            return VirtualPhysicalMesh(host_ids=host_ids,
                              host_info=host_info,
                              num_devices_per_host=num_devices_per_host,
                              gpu_info_per_node=gpu_info_per_node,
                              #devices=self.devices,
                              parent=self)
        else:
            return VirtualPhysicalMesh(host_ids=host_ids,
                                    host_info=host_info,
                                    num_devices_per_host=num_devices_per_host,
                                    gpu_info_per_node=gpu_info_per_node,
                                    parent=self)

    def profile_all(self, *args, **kwargs):
        """Profile computation and communication cost for all submesh shapes of
        this cluster."""
        return mesh_profiling.profile_all(self, *args, **kwargs)


# Global runtime objects
global_cluster: DeviceCluster = None
global_physical_mesh: PhysicalDeviceMesh = None
global_virtual_physical_mesh: VirtualPhysicalMesh = None

global_gpulet_mesh: GPUletMesh = None

def set_global_gpulet_mesh(mesh: GPUletMesh = None):
    if global_gpulet_mesh:
        raise NotImplementedError("function set_global_gpulet_mesh is only used for \
                                   initializing the global gpulet mesh.")





def init_global_cluster(cluster: str,
                        cluster_address: Optional[str] = None,
                        num_nodes: Optional[int] = None,
                        num_devices_per_node: Optional[int] = None,
                        namespace: Optional[str] = None):
    global global_cluster, global_physical_mesh, global_virtual_physical_mesh
    global global_gpulet_mesh

    if cluster == "local":
        global_physical_mesh = LocalPhysicalDeviceMesh()
    elif cluster == "ray":
        if not ray.is_initialized():
            ray_addr = cluster_address if cluster_address else "auto"
            print(f"Initializing ray with address {ray_addr}")
            ray.init(address=ray_addr,
                     ignore_reinit_error=True,
                     namespace=namespace)
        update_jax_platform("cpu")
        global_cluster = DeviceCluster(num_nodes, num_devices_per_node)
        global_virtual_physical_mesh = (
            global_cluster.get_virtual_physical_mesh())

        global_gpulet_mesh = (
            global_virtual_physical_mesh.get_gpulet_mesh())

        #global_virtual_physical_mesh.num_devices_per_host = 4
        #logger.warning("Changing num_devices of global virtual physical mesh for now.")


def shutdown_global_cluster():
    global global_cluster, global_physical_mesh, global_virtual_physical_mesh
    global global_gpulet_mesh

    if global_physical_mesh:
        global_physical_mesh.shutdown()
        global_physical_mesh = None

    if global_virtual_physical_mesh:
        if global_virtual_physical_mesh.launched_physical_mesh_group:
            global_virtual_physical_mesh.launched_physical_mesh_group.shutdown()
        global_virtual_physical_mesh = None

    if global_gpulet_mesh:
        global_gpulet_mesh = None
        GPUlet.gpulet_id = 0
        PhysicalGPU.gpu_id = 0

    global_cluster.delete_placement_group()
    global_cluster = None
    update_jax_platform("gpu")


def set_global_cluster(cluster: DeviceCluster):
    global global_cluster
    global_cluster = cluster


def get_global_cluster():
    return global_cluster


def set_global_physical_mesh(mesh: PhysicalDeviceMesh):
    global global_physical_mesh
    global_physical_mesh = mesh


def get_global_physical_mesh(create_if_not_exist=False):
    global global_physical_mesh

    if global_physical_mesh is None and create_if_not_exist:
        if global_cluster is None:
            # ray is not initialized, use local devices
            mesh = LocalPhysicalDeviceMesh()
        else:
            mesh = global_cluster.get_physical_mesh()
        global_physical_mesh = mesh

    return global_physical_mesh


def set_global_virtual_physical_mesh(mesh: VirtualPhysicalMesh):
    global global_virtual_physical_mesh
    global_virtual_physical_mesh = mesh


def get_global_virtual_physical_mesh():
    return global_virtual_physical_mesh


def get_global_gpulet_mesh():
    return global_gpulet_mesh


def set_seed(seed: int):
    global_config.runtime_random_seed = seed

    if global_physical_mesh:
        global_physical_mesh.set_runtime_random_seed(seed)
    if (global_virtual_physical_mesh and
            global_virtual_physical_mesh.launched_physical_mesh_group):
        global_virtual_physical_mesh.launched_physical_mesh_group.\
            set_runtime_random_seed(seed)


def get_global_num_devices():
    if global_virtual_physical_mesh:
        return global_virtual_physical_mesh.num_devices
    if global_physical_mesh:
        return global_physical_mesh.num_devices

    raise RuntimeError("Please call alpa.init first")


def create_and_record_cross_mesh_collective_communicators(
        meshes: Sequence[DistributedPhysicalDeviceMesh], key):
    workers = []
    device_strs = []
    for mesh in meshes:
        workers.extend(mesh.workers)
        device_strs.extend(mesh.device_strs)
    world_size = len(workers)
    backend = "nccl"
    group_name = ",".join(device_strs)
    refs = []
    for rank, worker in enumerate(workers):
        ref = worker.create_and_set_cross_mesh_communicators.remote(
            world_size, rank, backend, group_name, key)
        refs.append(ref)
    return refs


########################################
# Register ShardArg Handler
########################################
def _device_mesh_put(device_mesh, shards, num_batch, batch_dim):
    ary_refs, ary_uuids = create_remote_array_refs(device_mesh, num_batch)
    shard_step = device_mesh.num_devices_per_host
    for host_id in range(device_mesh.num_hosts):
        device_mesh.workers[host_id].put_buffers.remote(
            ary_uuids, shards[host_id * shard_step:(host_id + 1) * shard_step],
            num_batch, batch_dim)
    return ary_refs


def _device_mesh_put_dummy(array, device_mesh, indices, num_batch):
    ary_refs, ary_uuids = create_remote_array_refs(device_mesh, num_batch)
    step = device_mesh.num_devices_per_host * num_batch
    for host_id in range(device_mesh.num_hosts):
        device_mesh.workers[host_id].shard_and_put_non_zero_buffer.remote(
            ary_uuids, array.shape, array.dtype,
            indices[host_id * step:(host_id + 1) * step], num_batch)
    return ary_refs


def _shard_abstract_array(array,
                          device_mesh,
                          indices,
                          num_batch=1,
                          batch_dim=0):
    # pylint: disable=unused-argument
    assert global_config.use_dummy_value_for_benchmarking is True
    return _device_mesh_put_dummy(array, device_mesh, indices, num_batch)


def _shard_array(array, device_mesh, indices, num_batch=1, batch_dim=0):
    if global_config.use_dummy_value_for_benchmarking:
        return _device_mesh_put_dummy(array, device_mesh, indices, num_batch)
    else:
        # Create shards according to indices for a numpy array
        if array.shape == ():
            # need a special branch because np.ascontiguousarray does not
            # correctly preserve the shapes of rank-0 arrays.
            datas = [np.asarray(array)] * len(indices)
        else:
            datas = [np.ascontiguousarray(array[i]) for i in indices]
        if num_batch > 1:
            concate_datas = []
            for device_id in range(device_mesh.num_devices):
                mb = datas[device_id * num_batch:(device_id + 1) * num_batch]
                concate_datas.append(np.concatenate(mb, axis=batch_dim))
            datas = concate_datas
        return _device_mesh_put(device_mesh, datas, num_batch, batch_dim)


def _shard_device_array(array, device_mesh, indices, num_batch=1, batch_dim=0):
    if global_config.use_dummy_value_for_benchmarking:
        return _device_mesh_put_dummy(array, device_mesh, indices, num_batch)
    else:
        return _shard_array(np.asarray(array), device_mesh, indices, num_batch,
                            batch_dim)


def _shard_distributed_array(array,
                             device_mesh,
                             indices,
                             num_batch=1,
                             batch_dim=0):
    # Slow path: gather values to host and reshard
    return shard_arg_handlers[type(array._value)](array._value, device_mesh,
                                                  indices, num_batch, batch_dim)


shard_arg_handlers = {}  # Shard an argument to a distributed array
for a in array_types:
    shard_arg_handlers[a] = _shard_array
shard_arg_handlers[ShapedArray] = _shard_abstract_array
shard_arg_handlers[ShapeDtypeStruct] = _shard_abstract_array
shard_arg_handlers[xla._DeviceArray] = _shard_device_array
shard_arg_handlers[xla._CppDeviceArray] = _shard_device_array
shard_arg_handlers[DistributedArray] = _shard_distributed_array
shard_arg_handlers[ShardedDeviceArray] = _shard_distributed_array
