nohup python run_image_classification.py \
    --output_dir /data/dyf/output/vit-base-patch16-imagenette \
    --model_name_or_path /data/dyf/model_card/google/vit-base-patch16-224-in21k \
    --train_dir="/data/dyf/data/imagenette2/train" \
    --validation_dir="/data/dyf/data/imagenette2/val" \
    --num_train_epochs 1 \
    --num_micro_batches 2 \
    --learning_rate 1e-3 \
    --per_device_train_batch_size 16 \
    --per_device_eval_batch_size 16 \
    --overwrite_output_dir \
    --preprocessing_num_workers 8 \
    > vit_train_pipe_single.log 2>&1 & \