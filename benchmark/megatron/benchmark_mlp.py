import os

def run_cmd(cmd):
    print(cmd)
    return os.system(cmd)

benchmark_suite = [
    # Batch size, seq_len, hidden size, num_layers, num_heads, dp_size, tensor_mp_size,
    (32,          1024,    2304,        4,          2304//96,  4,       1),
    (32,          1024,    2304,        4,          2304//96,  2,       2),
    (32,          1024,    2304,        4,          2304//96,  1,       4),

    # Batch size, seq_len, hidden size, num_layers, num_heads, dp_size, tensor_mp_size,
    (8,           256,     5760,        4,          5760//96,  4,       1),
    (8,           256,     5760,        4,          5760//96,  2,       2),
    (8,           256,     5760,        4,          5760//96,  1,       4),
]

def benchmark_all():
    for case in benchmark_suite:
        nproc_per_node = 4
        case_str = str(case)
        ret = run_cmd('python3 -m torch.distributed.launch '
                     f'--nproc_per_node {nproc_per_node} '
                     'benchmark_mlp_one_case.py '
                     f'"{case_str}"')
        if ret != 0:
            return

if __name__ == "__main__":
    benchmark_all()

