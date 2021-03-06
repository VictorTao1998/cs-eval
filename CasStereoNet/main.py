from __future__ import print_function, division
import argparse
import os, sys, shutil
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import torchvision.utils as vutils
import torch.nn.functional as F
import numpy as np
import time
from tensorboardX import SummaryWriter
from datasets import __datasets__
from models import __models__, __loss__
from utils import *
import gc
from datasets.warp_ops import *
import cv2

cudnn.benchmark = True
assert torch.backends.cudnn.enabled, "Amp requires cudnn backend to be enabled."

parser = argparse.ArgumentParser(description='Cascade Stereo Network (CasStereoNet)')
parser.add_argument('--model', default='gwcnet-c', help='select a model structure', choices=__models__.keys())
parser.add_argument('--maxdisp', type=int, default=192, help='maximum disparity')

parser.add_argument('--dataset', required=True, help='dataset name', choices=__datasets__.keys())
parser.add_argument('--datapath', required=True, help='data path')
parser.add_argument('--depthpath', required=True, help='depth path')
parser.add_argument('--test_dataset', required=True, help='dataset name', choices=__datasets__.keys())
parser.add_argument('--test_datapath', required=True, help='data path')
parser.add_argument('--test_sim_datapath', required=True, help='data path')
parser.add_argument('--test_real_datapath', required=True, help='data path')
parser.add_argument('--trainlist', required=True, help='training list')
parser.add_argument('--testlist', required=True, help='testing list')
parser.add_argument('--sim_testlist', required=True, help='testing list')
parser.add_argument('--real_testlist', required=True, help='testing list')

parser.add_argument('--lr', type=float, default=0.001, help='base learning rate')
parser.add_argument('--batch_size', type=int, default=1, help='training batch size')
parser.add_argument('--test_batch_size', type=int, default=1, help='testing batch size')
parser.add_argument('--epochs', type=int, required=True, help='number of epochs to train')
parser.add_argument('--lrepochs', type=str, required=True, help='the epochs to decay lr: the downscale rate')

parser.add_argument('--logdir', required=True, help='the directory to save logs and checkpoints')
parser.add_argument('--loadckpt', help='load the weights from a specific checkpoint')
parser.add_argument('--resume', action='store_true', help='continue training the model')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')

parser.add_argument('--summary_freq', type=int, default=50, help='the frequency of saving summary')
parser.add_argument('--test_summary_freq', type=int, default=50, help='the frequency of saving summary')
parser.add_argument('--save_freq', type=int, default=1, help='the frequency of saving checkpoint')

parser.add_argument('--log_freq', type=int, default=50, help='log freq')
parser.add_argument('--eval_freq', type=int, default=1, help='eval freq')
parser.add_argument("--local_rank", type=int, default=0)

parser.add_argument('--mode', type=str, default="train", help='train or test mode')


parser.add_argument('--ndisps', type=str, default="48,24", help='ndisps')
parser.add_argument('--disp_inter_r', type=str, default="4,1", help='disp_intervals_ratio')
parser.add_argument('--dlossw', type=str, default="0.5,2.0", help='depth loss weight for different stage')
parser.add_argument('--cr_base_chs', type=str, default="32,32,16", help='cost regularization base channels')
parser.add_argument('--grad_method', type=str, default="detach", choices=["detach", "undetach"], help='predicted disp detach, undetach')


parser.add_argument('--using_ns', action='store_true', help='using neighbor search')
parser.add_argument('--ns_size', type=int, default=3, help='nb_size')

parser.add_argument('--crop_height', type=int, required=True, help="crop height")
parser.add_argument('--crop_width', type=int, required=True, help="crop width")
parser.add_argument('--test_crop_height', type=int, required=True, help="crop height")
parser.add_argument('--test_crop_width', type=int, required=True, help="crop width")

parser.add_argument('--using_apex', action='store_true', help='using apex, need to install apex')
parser.add_argument('--sync_bn', action='store_true',help='enabling apex sync BN.')
parser.add_argument('--opt-level', type=str, default="O0")
parser.add_argument('--keep-batchnorm-fp32', type=str, default=None)
parser.add_argument('--loss-scale', type=str, default=None)

parser.add_argument('--ground', action='store_true', help='include ground pixel')







# parse arguments
args = parser.parse_args()
os.makedirs(args.logdir, exist_ok=True)

#using sync_bn by using nvidia-apex, need to install apex.
if args.sync_bn:
    assert args.using_apex, "must set using apex and install nvidia-apex"
if args.using_apex:
    try:
        from apex.parallel import DistributedDataParallel as DDP
        from apex.fp16_utils import *
        from apex import amp, optimizers
        from apex.multi_tensor_apply import multi_tensor_applier
    except ImportError:
        raise ImportError("Please install apex from https://www.github.com/nvidia/apex to run this example.")

#dis
num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
is_distributed = num_gpus > 1
args.is_distributed = is_distributed

if is_distributed:
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(
        backend="nccl", init_method="env://"
    )
    synchronize()

#set seed
set_random_seed(args.seed)

if (not is_distributed) or (dist.get_rank() == 0):
    # create summary logger
    print("argv:", sys.argv[1:])
    print_args(args)
    print("creating new summary file")
    logger = SummaryWriter(args.logdir)

# model
model = __models__[args.model](
                            maxdisp=args.maxdisp,
                            ndisps=[int(nd) for nd in args.ndisps.split(",") if nd],
                            disp_interval_pixel=[float(d_i) for d_i in args.disp_inter_r.split(",") if d_i],
                            cr_base_chs=[int(ch) for ch in args.cr_base_chs.split(",") if ch],
                            grad_method=args.grad_method,
                            using_ns=args.using_ns,
                            ns_size=args.ns_size
                           )
if args.sync_bn:
    import apex
    print("using apex synced BN")
    model = apex.parallel.convert_syncbn_model(model)

model_loss = __loss__[args.model]
model.cuda()
print('Number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()])))

#optimizer
optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999))

# load parameters
start_epoch = 0
if args.resume:
    # find all checkpoints file and sort according to epoch id
    all_saved_ckpts = [fn for fn in os.listdir(args.logdir) if (fn.endswith(".ckpt") and not fn.endswith("best.ckpt"))]
    all_saved_ckpts = sorted(all_saved_ckpts, key=lambda x: int(x.split('_')[-1].split('.')[0]))
    # use the latest checkpoint file
    loadckpt = os.path.join(args.logdir, all_saved_ckpts[-1])
    print("loading the lastest model in logdir: {}".format(loadckpt))
    state_dict = torch.load(loadckpt, map_location=torch.device("cpu"))
    model.load_state_dict(state_dict['model'])
    optimizer.load_state_dict(state_dict['optimizer'])
    start_epoch = state_dict['epoch'] + 1
elif args.loadckpt:
    # load the checkpoint file specified by args.loadckpt
    print("loading model {}".format(args.loadckpt))
    state_dict = torch.load(args.loadckpt, map_location=torch.device("cpu"))
    model.load_state_dict(state_dict['model'])
print("start at epoch {}".format(start_epoch))

if args.using_apex:
    # Initialize Amp
    model, optimizer = amp.initialize(model, optimizer,
                                      opt_level=args.opt_level,
                                      keep_batchnorm_fp32=args.keep_batchnorm_fp32,
                                      loss_scale=args.loss_scale
                                      )

#conver model to dist
if is_distributed:
    print("Dist Train, Let's use", torch.cuda.device_count(), "GPUs!")
    model = torch.nn.parallel.DistributedDataParallel(
        model, device_ids=[args.local_rank], output_device=args.local_rank,
        # find_unused_parameters=False,
        # this should be removed if we update BatchNorm stats
        # broadcast_buffers=False,
    )
else:
    if torch.cuda.is_available():
        print("Let's use", torch.cuda.device_count(), "GPUs!")
        model = nn.DataParallel(model)


# dataset, dataloader


StereoDataset = __datasets__[args.dataset]
Test_StereoDataset = __datasets__[args.test_dataset]
Test_sim_StereoDataset = __datasets__[args.test_dataset]
Test_real_StereoDataset = __datasets__[args.test_dataset]

train_dataset = StereoDataset(args.datapath, args.depthpath, args.trainlist, True,
                              crop_height=args.crop_height, crop_width=args.crop_width,
                              test_crop_height=args.test_crop_height, test_crop_width=args.test_crop_width,
                              left_img="0128_irL_denoised_half.png", right_img="0128_irR_denoised_half.png", args=args)
test_dataset = Test_StereoDataset(args.test_datapath, args.depthpath, args.testlist, False,
                             crop_height=args.crop_height, crop_width=args.crop_width,
                             test_crop_height=args.test_crop_height, test_crop_width=args.test_crop_width,
                             left_img="0128_irL_denoised_half.png", right_img="0128_irR_denoised_half.png", args=args)
sim_test_dataset = Test_sim_StereoDataset(args.test_sim_datapath, args.depthpath, args.sim_testlist, False,
                             crop_height=args.crop_height, crop_width=args.crop_width,
                             test_crop_height=args.test_crop_height, test_crop_width=args.test_crop_width,
                             left_img="0128_irL_denoised_half.png", right_img="0128_irR_denoised_half.png", args=args)
real_test_dataset = Test_real_StereoDataset(args.test_real_datapath, args.depthpath, args.real_testlist, False,
                             crop_height=args.crop_height, crop_width=args.crop_width,
                             test_crop_height=args.test_crop_height, test_crop_width=args.test_crop_width,
                             left_img="1024_irL_real_1080.png", right_img="1024_irR_real_1080.png", args=args)

if is_distributed:
    train_sampler = torch.utils.data.DistributedSampler(train_dataset, num_replicas=dist.get_world_size(),
                                                        rank=dist.get_rank())
    test_sampler = torch.utils.data.DistributedSampler(test_dataset, num_replicas=dist.get_world_size(),
                                                       rank=dist.get_rank())

    TrainImgLoader = torch.utils.data.DataLoader(train_dataset, args.batch_size, sampler=train_sampler, num_workers=1,
                                                 drop_last=True, pin_memory=True)
    TestImgLoader = torch.utils.data.DataLoader(test_dataset, args.test_batch_size, sampler=test_sampler, num_workers=1,
                                                drop_last=False, pin_memory=True)

else:
    TrainImgLoader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size,
                                                 shuffle=True, num_workers=8, drop_last=True)

    TestImgLoader = torch.utils.data.DataLoader(test_dataset, batch_size=args.test_batch_size,
                                                shuffle=False, num_workers=4, drop_last=False)

    SimTestImgLoader = torch.utils.data.DataLoader(sim_test_dataset, batch_size=args.test_batch_size,
                                                shuffle=False, num_workers=4, drop_last=False)

    RealTestImgLoader = torch.utils.data.DataLoader(real_test_dataset, batch_size=args.test_batch_size,
                                                shuffle=False, num_workers=4, drop_last=False)


num_stage = len([int(nd) for nd in args.ndisps.split(",") if nd])

def train():
    avg_test_scalars = None
    Cur_D1 = 1
    for epoch_idx in range(start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch_idx, args.lr, args.lrepochs)

        # training
        for batch_idx, sample in enumerate(TrainImgLoader):

            global_step = len(TrainImgLoader) * epoch_idx + batch_idx
            start_time = time.time()
            do_summary = global_step % args.summary_freq == 0
            loss, scalar_outputs, image_outputs = train_sample(sample, compute_metrics=do_summary)
            if (not is_distributed) or (dist.get_rank() == 0):
                if do_summary:
                    save_scalars(logger, 'train', scalar_outputs, global_step)
                    save_images(logger, 'train', image_outputs, global_step)
                    #save_texts(logger, 'train', text_outputs, global_step)
                del scalar_outputs, image_outputs
                if batch_idx % args.log_freq == 0:
                    if isinstance(loss, (list, tuple)):
                        loss = loss[0]
                    print('Epoch {}/{}, Iter {}/{}, lr {:.5f}, train loss = {:.3f}, time = {:.3f}'.format(epoch_idx,
                                                                                           args.epochs,
                                                                                           batch_idx,
                                                                                           len(TrainImgLoader),
                                                                                           optimizer.param_groups[0]["lr"],
                                                                                           loss, time.time() - start_time))
        # saving checkpoints
        if (epoch_idx + 1) % args.save_freq == 0:
            if (not is_distributed) or (dist.get_rank() == 0):
                checkpoint_data = {'epoch': epoch_idx, 'model': model.module.state_dict(), 'optimizer': optimizer.state_dict()}
                save_filename = "{}/checkpoint_{:0>6}.ckpt".format(args.logdir, epoch_idx)
                torch.save(checkpoint_data, save_filename)
        gc.collect()

        if (epoch_idx % args.eval_freq == 0) or (epoch_idx == args.epochs - 1):
            # testing
            avg_test_scalars = AverageMeterDict()
            for batch_idx, sample in enumerate(TestImgLoader):

                global_step = len(TestImgLoader) * epoch_idx + batch_idx
                start_time = time.time()
                do_summary = global_step % args.test_summary_freq == 0
                loss, scalar_outputs, image_outputs, _, _ = test_sample(sample, compute_metrics=do_summary)
                if (not is_distributed) or (dist.get_rank() == 0):
                    if do_summary:
                        save_scalars(logger, 'test', scalar_outputs, global_step)
                        save_images(logger, 'test', image_outputs, global_step)
                    avg_test_scalars.update(scalar_outputs)
                    del scalar_outputs, image_outputs
                    if batch_idx % args.log_freq == 0:
                        if isinstance(loss, (list, tuple)):
                            loss = loss[0]
                        print('Epoch {}/{}, Iter {}/{}, test loss = {:.3f}, time = {:3f}'.format(epoch_idx, args.epochs,
                                                                                             batch_idx,
                                                                                             len(TestImgLoader), loss,
                                                                                             time.time() - start_time))


            if (not is_distributed) or (dist.get_rank() == 0):
                avg_test_scalars = avg_test_scalars.mean()
                save_scalars(logger, 'fulltest', avg_test_scalars, len(TrainImgLoader) * (epoch_idx + 1))
                print("avg_test_scalars", avg_test_scalars)



            # saving bset checkpoints
            if (not is_distributed) or (dist.get_rank() == 0):
                if avg_test_scalars is not None:
                    New_D1 = avg_test_scalars["D1"][0]
                    if New_D1 < Cur_D1:
                        Cur_D1 = New_D1
                        #save
                        checkpoint_data = {'epoch': epoch_idx, 'model': model.module.state_dict(),
                                           'optimizer': optimizer.state_dict()}
                        save_filename = "{}/checkpoint_best.ckpt".format(args.logdir)
                        torch.save(checkpoint_data, save_filename)
                        print("Best Checkpoint epoch_idx:{}".format(epoch_idx))

            gc.collect()


# train one sample
def train_sample(sample, compute_metrics=False):
    model.train()

    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    imgL = imgL.cuda()
    imgR = imgR.cuda()
    disp_gt = disp_gt.cuda()

    #disp_gt_b = disp_gt
    #print(disp_gt.shape)
    disp_gt_t = disp_gt.reshape((2,1,256,512))
    disparity_L_from_R = apply_disparity_cu(disp_gt_t, disp_gt_t.int())
    #disp_gt = disparity_L_from_R.reshape((1,2,256,512))
    disp_gt = disparity_L_from_R.reshape((2,256,512))

    disp_gt = cv2.medianBlur(disp_gt.cpu().numpy(),3)

    disp_gt = torch.from_numpy(disp_gt).cuda()
    #print(disp_gt.shape)
    #disp_gt_a = disp_gt

    optimizer.zero_grad()

    outputs = model(imgL, imgR)
    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    loss = model_loss(outputs, disp_gt, mask, dlossw=[float(e) for e in args.dlossw.split(",") if e])

    outputs_stage = outputs["stage{}".format(num_stage)]
    disp_ests = [outputs_stage["pred1"], outputs_stage["pred2"], outputs_stage["pred3"]]

    scalar_outputs = {"loss": loss}
    image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR}
    #text_outputs = {}


    if compute_metrics:
        with torch.no_grad():
            image_outputs["errormap"] = [disp_error_image_func.apply(disp_est, disp_gt) for disp_est in disp_ests]
            scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
            scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
            scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests]
            scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests]
            scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests]
            #text_outputs["before_warp"] = [str(disp_gt_b)]
            #text_outputs["after_warp"] = [str(disp_gt_a)]


    if is_distributed and args.using_apex:
        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()
    else:
        loss.backward()
    optimizer.step()

    if is_distributed:
        scalar_outputs = reduce_scalar_outputs(scalar_outputs)

    return tensor2float(scalar_outputs["loss"]), tensor2float(scalar_outputs), image_outputs


# test one sample
@make_nograd_func
def test_sample(sample, compute_metrics=True):
    if is_distributed:
        model_eval = model.module
    else:
        model_eval = model
    model_eval.eval()

    imgL, imgR, disp_gt, dep_gt, f, b, label, obj_ids = sample['left'], sample['right'], sample['disparity'], sample['depth'], sample['f'], sample['baseline'], sample['label'], sample['obj_ids']
    imgL = imgL.cuda()
    imgR = imgR.cuda()
    disp_gt = disp_gt.cuda()

    #print(disp_gt.shape)
    disp_gt_t = disp_gt.reshape((1,1,768,1248))
    #disp_gt_rgb = disp_rgb.reshape((1,1,540,960)).cuda()
    #label = label.reshape((1,1,540,960)).cuda()
    #print(torch.max(disp_gt_t), torch.max(disp_gt_t.int()), torch.min(disp_gt_t), torch.min(disp_gt_t.int()))
    #print(torch.max(label), torch.max(disp_gt_rgb.int()), torch.min(label), torch.min(disp_gt_rgb.int()))

    disparity_L_from_R = apply_disparity_cu(disp_gt_t, disp_gt_t.int())
    #disparity_L_from_R = torch.ones((768,1248))



    disp_gt = disparity_L_from_R.reshape((768,1248)).cpu().numpy()
    #label_rgb = label_rgb.reshape((1,540,960)).cuda()

    disp_gt = cv2.medianBlur(disp_gt,3)

    disp_gt = torch.from_numpy(disp_gt).cuda().reshape((1,768,1248))


    outputs = model_eval(imgL, imgR)

    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    #print(mask.reshape)
    loss = torch.tensor(0, dtype=imgL.dtype, device=imgL.device, requires_grad=False) #model_loss(outputs, disp_gt, mask, dlossw=[float(e) for e in args.dlossw.split(",") if e])

    outputs_stage = outputs["stage{}".format(num_stage)]
    disp_ests = [outputs_stage["pred"]]

    scalar_outputs = {"loss": loss}
    #obj_err = np.zeros(17)
    obj_ct = np.zeros(17, dtype=int)

    #image_outputs = {"disp_est": disp_ests, "disp_gt": disp_gt, "imgL": imgL, "imgR": imgR, "label": label_rgb}


    depest = disp_ests[0].cpu().numpy()[0]
    depest = depest[228:,:960]
    dispgt = disp_gt.cpu().numpy()[0]
    dispgt = dispgt[228:,:960]

    dep_gt_c = dep_gt.cpu().numpy()[0]
    obj_ids = obj_ids.cpu().numpy()[0]
    label = label[0].cpu().numpy()[0]
    #print("dep_gt_c ", dep_gt_c.shape)
    #print("obj_ids ", obj_ids.shape)
    #print("label ", label.shape)

    obj_ct[obj_ids] = 1


    #label_rgb = apply_disparity_cu(label, disp_gt_rgb.int())
    #label_rgb = label_rgb.reshape((1,540,960))
    #label = warp(label, disp_rgb)
    #label = label.cpu().numpy()[0]
    #print(label.shape)
    #print(dispgt.shape)
    if args.ground:
        maskest = (dispgt < args.maxdisp) & (dispgt > 0)
    else:
        maskest = (dispgt < args.maxdisp) & (dispgt > 0) & (dep_gt_c <= 1250) & (dep_gt_c > 0)
    #print("mask:", np.sum(maskest))
    #print("back:", np.sum(label == 18))
    maskest2 = (depest == 0)
    depest = np.divide(f*b, depest)
    depest[maskest2] = 0

    obj_avg_err = obj_analysis(label, obj_ids, dep_gt_c, np.asarray(depest))
    #print(depest.dtype, dep_gt.dtype, depest.shape, dep_gt.shape)
    dep_err_map = np.abs(np.asarray(depest) - np.asarray(dep_gt[0]))
    dep_err = np.mean(dep_err_map[maskest])

    dep_2 = np.sum(dep_err_map[maskest] > 2)/np.sum(maskest)
    dep_4 = np.sum(dep_err_map[maskest] > 4)/np.sum(maskest)
    dep_8 = np.sum(dep_err_map[maskest] > 8)/np.sum(maskest)




    disp_ests_bad = disp_ests[0].cpu().numpy()[0]
    disp_ests_gt_bad = disp_gt.cpu().numpy()[0]
    disp_ests_bad = disp_ests_bad[228:,:960]
    disp_ests_gt_bad = disp_ests_gt_bad[228:,:960]

    #print(disp_ests_bad.shape, disp_ests_gt_bad.shape)
    bad1 = np.sum(np.abs(disp_ests_bad - disp_ests_gt_bad)[maskest] > 1)/np.sum(maskest)
    bad2 = np.sum(np.abs(disp_ests_bad - disp_ests_gt_bad)[maskest] > 2)/np.sum(maskest)

    #dep_gt = disp_gt

    scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests]
    scalar_outputs["EPE"] = [EPE_metric(disp_est[:,228:,:960], disp_gt[:,228:,:960], torch.tensor(maskest).reshape((1,540,960))) for disp_est in disp_ests]
    scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests]
    scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests]
    scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests]
    scalar_outputs["bad1.0"] = [bad1]
    scalar_outputs["bad2.0"] = [bad2]
    scalar_outputs["dep_err"] = [dep_err]
    scalar_outputs["dep2"] = [dep_2]
    scalar_outputs["dep4"] = [dep_4]
    scalar_outputs["dep8"] = [dep_8]

    feature_outputs = [outputs["fea"][:,i,:,:] for i in range(32)]

    label = torch.tensor(label, dtype=float).reshape((1,540,960))
    image_outputs = {"disp_est": disp_ests[0][:,228:,:960], "disp_gt": disp_gt[:,228:,:960], "imgL": imgL[:,:,228:,:960], "imgR": imgR[:,:,228:,:960], "label": label, "feature": feature_outputs}

    if compute_metrics:
        image_outputs["errormap"] = [(disp_error_image_func.apply(disp_est, disp_gt))[:,:,228:,:960] for disp_est in disp_ests]

    if is_distributed:
        scalar_outputs = reduce_scalar_outputs(scalar_outputs)

    return tensor2float(scalar_outputs["loss"]), tensor2float(scalar_outputs), image_outputs, obj_avg_err, obj_ct

def warp(label, disp):
    label = torch.tensor(label).reshape((1,1,540,960)).float().cuda()
    disp_gt_rgb = disp.reshape((1,1,540,960)).cuda()
    label_rgb = apply_disparity_cu(label, -disp_gt_rgb.int())
    label_rgb = label_rgb.reshape((1,540,960))
    label = label_rgb.cpu().numpy()[0]
    return label

def obj_analysis(label, obj_ids, dep_gt, dep_est):
    obj_avg_err = np.zeros(17, dtype=int)
    for id in obj_ids:
        mask = (label == id)
        obj_err = np.mean(np.abs(dep_gt[mask] - dep_est[mask]))
        #print(id," ",obj_err)
        if np.sum(mask) == 0:
            obj_err = 0
        obj_avg_err[id] = obj_err
    return obj_avg_err

def test_all():
    # sim testing
    avg_test_scalars = AverageMeterDict()
    obj_dict = np.zeros(17)
    obj_num = np.zeros(17, dtype=int)
    for batch_idx, sample in enumerate(SimTestImgLoader):
        start_time = time.time()
        do_summary = batch_idx % args.test_summary_freq == 0
        loss, scalar_outputs, image_outputs, obj_avg_err, obj_ct = test_sample(sample, compute_metrics=True)
        obj_dict = obj_dict + obj_avg_err
        obj_num = obj_num + obj_ct


        if (not is_distributed) or (dist.get_rank() == 0):
            avg_test_scalars.update(scalar_outputs)
            if do_summary:
                save_scalars(logger, 'test_sim', scalar_outputs, batch_idx)
                save_images(logger, 'test_sim', image_outputs, batch_idx)
            del scalar_outputs, image_outputs
            if batch_idx % args.log_freq == 0:
                if isinstance(loss, (list, tuple)):
                    loss = loss[0]
                print('Iter {}/{}, test loss = {:.3f}, time = {:3f}'.format(
                                                                             batch_idx,
                                                                             len(SimTestImgLoader), loss,
                                                                             time.time() - start_time))
    average_perobj = obj_dict/obj_num
    print("per obj sim : ", average_perobj)
    print("per obj cnt : ", obj_num)
    if (not is_distributed) or (dist.get_rank() == 0):
        avg_test_scalars = avg_test_scalars.mean()
        save_scalars(logger, 'fulltest_sim', avg_test_scalars, len(SimTestImgLoader))
        print("avg_test_scalars_sim", avg_test_scalars)

    # real testing
    avg_test_scalars = AverageMeterDict()
    obj_dict = np.zeros(17)
    obj_num = np.zeros(17, dtype=int)
    for batch_idx, sample in enumerate(RealTestImgLoader):
        start_time = time.time()
        do_summary = batch_idx % args.test_summary_freq == 0
        loss, scalar_outputs, image_outputs, obj_avg_err, obj_ct = test_sample(sample, compute_metrics=True)
        obj_dict = obj_dict + obj_avg_err
        obj_num = obj_num + obj_ct


        if (not is_distributed) or (dist.get_rank() == 0):
            avg_test_scalars.update(scalar_outputs)
            if do_summary:
                save_scalars(logger, 'test_real', scalar_outputs, batch_idx)
                save_images(logger, 'test_real', image_outputs, batch_idx)
            del scalar_outputs, image_outputs
            if batch_idx % args.log_freq == 0:
                if isinstance(loss, (list, tuple)):
                    loss = loss[0]
                print('Iter {}/{}, test loss = {:.3f}, time = {:3f}'.format(
                                                                             batch_idx,
                                                                             len(RealTestImgLoader), loss,
                                                                             time.time() - start_time))
    average_perobj = obj_dict/obj_num
    print("per obj real : ", average_perobj)
    print("per obj cnt : ", obj_num)
    if (not is_distributed) or (dist.get_rank() == 0):
        avg_test_scalars = avg_test_scalars.mean()
        save_scalars(logger, 'fulltest_real', avg_test_scalars, len(RealTestImgLoader))
        print("avg_test_scalars_real", avg_test_scalars)


if __name__ == '__main__':
    if args.mode == 'train':
        train()
    elif args.mode == 'test':
        test_all()
