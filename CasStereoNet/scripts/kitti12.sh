#!/usr/bin/env bash
#set -x
export PYTHONWARNINGS="ignore"

save_path="/cephfs/jianyu/mtcaseval/checkpoint"

if [ ! -d $save_path ];then
    mkdir -p $save_path
fi

DATAPATH="/cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training"
pip install --upgrade pip setuptools wheel
pip install -r /cephfs/jianyu/mtcaseval/requirements.txt
python -m torch.distributed.launch --nproc_per_node=1 /cephfs/jianyu/mtcaseval/CasStereoNet/main.py --dataset kitti \
    --datapath /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training --trainlist /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training_lists/all_train.txt --testlist /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training_lists/all_val.txt \
    --test_datapath /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training --test_dataset kitti \
    --epochs 300 --lrepochs "200:10" \
    --crop_width 512  --crop_height 256 --test_crop_width 1248  --test_crop_height 768 \
    --ndisp "48,24" --disp_inter_r "4,1" --dlossw "0.5,2.0"  --using_ns --ns_size 3 \
    --model gwcnet-c --logdir "/cephfs/jianyu/mtcaseval/checkpoint"  --ndisps "48,24" \
    --disp_inter_r "4,1"   --batch_size 2 --mode train
