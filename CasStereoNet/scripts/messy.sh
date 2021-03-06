#!/usr/bin/env bash
#set -x
export PYTHONWARNINGS="ignore"

save_path="/cephfs/jianyu/eval/cs_eval"

if [ ! -d $save_path ];then
    mkdir -p $save_path
fi


python -m torch.distributed.launch --nproc_per_node=1 /cephfs/jianyu/cs_eval_fv/CasStereoNet/main.py \
    --dataset messy_table \
    --test_dataset messy_table \
    --datapath /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training \
    --trainlist /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training_lists/all_train.txt \
    --test_datapath /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training \
    --testlist /cephfs/datasets/iccv_pnp/messy-table-dataset/v9/training_lists/all_val.txt \
    --test_sim_datapath /cephfs/datasets/iccv_pnp/messy-table-dataset/real_v9/training \
    --sim_testlist /cephfs/datasets/iccv_pnp/messy-table-dataset/real_v9/training_lists/all.txt \
    --test_real_datapath /cephfs/datasets/iccv_pnp/real_data_v9/ \
    --real_testlist /cephfs/datasets/iccv_pnp/messy-table-dataset/real_v9/training_lists/all.txt \
    --depthpath /cephfs/datasets/iccv_pnp/messy-table-dataset/real_v9/training \
    --epochs 300 \
    --lrepochs "200:10" \
    --crop_width 512  \
    --crop_height 256 \
    --test_crop_width 1248  \
    --test_crop_height 768 \
    --ndisp "48,24" \
    --disp_inter_r "4,1" \
    --dlossw "0.5,2.0"  \
    --using_ns \
    --ns_size 3 \
    --model gwcnet-c \
    --logdir "/cephfs/jianyu/eval/cs_eval_dr_fv"  \
    --ndisps "48,24" \
    --disp_inter_r "4,1"  \
    --batch_size 2 \
    --mode test \
    --summary_freq 500 \
    --test_summary_freq 50 \
    --loadckpt "/cephfs/jianyu/eval/cs_train_f_5/checkpoint_best.ckpt"
