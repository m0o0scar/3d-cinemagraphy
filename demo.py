import argparse
import os.path
import json

import imageio
import torch.utils.data.distributed
import torchvision
from torchvision.transforms import Compose, Normalize, ToTensor, InterpolationMode
from tqdm import tqdm
from PIL import Image

from lib.config import load_config
from lib.utils.general_utils import *
from lib.model.inpaint.model import SpaceTimeAnimationModel
from lib.utils.render_utils import *
from lib.model.motion.motion_model import SPADEUnetMaskMotion
from lib.model.motion.sync_batchnorm import convert_model
from lib.renderer import ImgRenderer
from lib.model.inpaint.inpainter import Inpainter
from lib.utils.data_utils import resize_img
from third_party.DPT.run_monodepth import run_dpt


def generate_mask_hints_from_user(args, config):
    mask = torch.zeros((1, 1, 768, 768))
    hint = torch.zeros((1, 2, 768, 768))
    return mask, hint


def get_input_data(args, config, video_out_folder, ds_factor=1):
    motion_input_transform = Compose(
        [
            torchvision.transforms.Resize((config['motionH'], config['motionW']),
                                          InterpolationMode.BICUBIC),
            ToTensor(),
            Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )
    to_tensor = ToTensor()

    try:
        img_file = os.path.join(args.input_dir, 'image.png')
        motion_rgb = Image.open(img_file)
    except:
        img_file = os.path.join(args.input_dir, 'image.jpg')
        motion_rgb = Image.open(img_file)
    motion_rgb = motion_input_transform(motion_rgb)
    mask, hints = generate_mask_hints_from_user(args, config)

    dpt_out_dir = os.path.join(video_out_folder, 'dpt_depth')

    src_img = imageio.imread(img_file) / 255.
    src_img = resize_img(src_img, ds_factor)

    h, w = src_img.shape[:2]

    run_dpt(
        input_path=args.input_dir,
        output_path=dpt_out_dir,
        model_path=args.dpt_model_path,
        model_type=args.dpt_model_type,
        optimize=False
    )
    disp_file = os.path.join(dpt_out_dir, 'image.png')

    src_disp = imageio.imread(disp_file) / 65535.
    src_disp = remove_noise_in_dpt_disparity(src_disp)

    src_depth = 1. / np.maximum(src_disp, 1e-6)

    src_depth = resize_img(src_depth, ds_factor)

    intrinsic = np.array([[max(h, w), 0, w // 2],
                          [0, max(h, w), h // 2],
                          [0, 0, 1]])

    pose = np.eye(4)

    return {
        'motion_rgbs': motion_rgb[None, ...],
        'src_img': to_tensor(src_img).float()[None],
        'src_depth': to_tensor(src_depth).float()[None],
        'hints': hints[0],
        'mask': mask[0],
        'intrinsic': torch.from_numpy(intrinsic).float()[None],
        'pose': torch.from_numpy(pose).float()[None],
        'scale_shift': torch.tensor([1., 0.]).float()[None],
        'src_rgb_file': [img_file],
    }


def render(args):
    device = "cuda:{}".format(args.local_rank)

    # set up folder
    video_out_folder = os.path.join(args.input_dir, 'output')
    os.makedirs(video_out_folder, exist_ok=True)

    # load config
    check_file(args.config)
    config = load_config(args.config)

    data = get_input_data(args, config['data'], video_out_folder, ds_factor=args.ds_factor)
    torch.cuda.empty_cache()

    ###########################################################################
    """ model """

    model = SpaceTimeAnimationModel(args, config)
    if model.start_step == 0:
        raise Exception('no pretrained model found! please check the model path.')

    scene_flow_estimator = SPADEUnetMaskMotion(config['generator']).to(device)
    scene_flow_estimator = convert_model(scene_flow_estimator)
    scene_flow_estimator_weight = torch.load('ckpts/sceneflow_model.pth',
                                             map_location=torch.device(device))
    scene_flow_estimator.load_state_dict(scene_flow_estimator_weight['netG'])
    inpainter = Inpainter(device=device)
    renderer = ImgRenderer(args, config, model, scene_flow_estimator, inpainter, device)

    ###########################################################################
    """ render """

    model.switch_to_eval()
    with torch.no_grad():
        renderer.process_data(data)

        coord, flow, pts_src, featmaps_src, rgba_layers_src, depth_layers_src, mask_layers_src = \
            renderer.compute_flow_and_inpaint()
        flow = flow / args.flow_scale

        num_frames = [args.n_frames]
        # video_paths = ['up-down', 'zoom-in', 'side', 'circle']
        video_paths = ['side']
        Ts = [
            define_camera_path(num_frames[0], -args.x_motion, 0, 0, path_type='double-straight-line', return_t_only=True),
        ]
        crop = 32
        kernel = torch.ones(5, 5, device=device)

        for j, T in enumerate(Ts):
            T = torch.from_numpy(T).float().to(renderer.device)
            time_steps = range(0, num_frames[j])
            start_index = torch.tensor([0]).to(device)
            end_index = torch.tensor([num_frames[j] - 1]).to(device)
            frames = []
            for middle_index, t_step in tqdm(enumerate(time_steps), total=len(time_steps), ncols=150,
                                             desc='generating video of {} camera trajectory'.format(video_paths[j])):
                middle_index = torch.tensor([middle_index]).to(device)
                time = ((middle_index.float() - start_index.float()).float() / (
                        end_index.float() - start_index.float() + 1.0).float()).item()

                flow_f = renderer.euler_integration(flow, middle_index.long() - start_index.long())
                flow_b = renderer.euler_integration(-flow, end_index.long() + 1 - middle_index.long())
                flow_f = flow_f.permute(0, 2, 3, 1)
                flow_b = flow_b.permute(0, 2, 3, 1)

                _, all_pts_f, _, all_rgbas_f, _, all_feats_f, \
                    all_masks_f, all_optical_flow_f = \
                    renderer.compute_scene_flow_for_motion(coord, torch.inverse(renderer.pose), renderer.src_img,
                                                           rgba_layers_src, featmaps_src, pts_src, depth_layers_src,
                                                           mask_layers_src, flow_f, kernel, with_inpainted=True)
                _, all_pts_b, _, all_rgbas_b, _, all_feats_b, \
                    all_masks_b, all_optical_flow_b = \
                    renderer.compute_scene_flow_for_motion(coord, torch.inverse(renderer.pose), renderer.src_img,
                                                           rgba_layers_src, featmaps_src, pts_src, depth_layers_src,
                                                           mask_layers_src, flow_b, kernel, with_inpainted=True)

                all_pts_flowed = torch.cat(all_pts_f + all_pts_b)
                all_rgbas_flowed = torch.cat(all_rgbas_f + all_rgbas_b)
                all_feats_flowed = torch.cat(all_feats_f + all_feats_b)
                all_masks = torch.cat(all_masks_f + all_masks_b)
                all_side_ids = torch.zeros_like(all_masks.squeeze(), dtype=torch.long)
                num_pts_2 = sum([len(x) for x in all_pts_b])
                all_side_ids[-num_pts_2:] = 1

                pred_img, _, meta = renderer.render_pcd(all_pts_flowed,
                                                        all_rgbas_flowed,
                                                        all_feats_flowed,
                                                        all_masks, all_side_ids,
                                                        t=T[middle_index.item()],
                                                        time=time,
                                                        t_step=t_step,
                                                        path_type=video_paths[j])

                frame = (255. * pred_img.detach().cpu().squeeze().permute(1, 2, 0).numpy()).astype(np.uint8)
                frame = frame[crop:-crop, crop:-crop]
                frames.append(frame)

            video_out_file = os.path.join(video_out_folder,
                                          f'{video_paths[j]}_flow_scale={args.flow_scale}.mp4')

            imageio.mimwrite(video_out_file, frames, fps=25, quality=8)

        print(f'space-time videos have been saved in {video_out_folder}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    ########## general ##########
    parser.add_argument('-c', '--config', type=str, help='config file path')
    parser.add_argument('--input_dir', type=str, help='input folder that contains src images', required=True)
    parser.add_argument('-j', '--num_workers', default=8, type=int, metavar='N',
                        help='number of data loading workers (default: 8)')
    parser.add_argument('--distributed', action='store_true', help='if use distributed training')
    parser.add_argument('--local_rank', type=int, default=0, help='rank for distributed training')

    parser.add_argument('--save_frames', action='store_true', help='if save frames')
    parser.add_argument('--correct_inpaint_depth', action='store_true',
                        help='use this option to correct the depth of inpainting area to prevent occlusion')
    parser.add_argument("--flow_scale", type=float, default=1.0,
                        help='flow scale that used to control the speed of fluid')
    parser.add_argument("--ds_factor", type=float, default=1.0,
                        help='downsample factor for the input images')

    ########## checkpoints ##########
    parser.add_argument("--ckpt_path", type=str, default='',
                        help='specific weights file to reload')
    parser.add_argument("--no_reload", action='store_true',
                        help='do not reload weights from saved ckpt')
    parser.add_argument("--no_load_opt", action='store_true',
                        help='do not load optimizer when reloading')
    parser.add_argument("--no_load_scheduler", action='store_true',
                        help='do not load scheduler when reloading')

    parser.add_argument("--dpt_model_path", type=str, default='ckpts/dpt_beit_large_384.pt', help='specific depth estimation weights file to load')
    parser.add_argument("--dpt_model_type", type=str, default='dpt_large', help='specific depth estimation model type')
    parser.add_argument("--n_frames", type=int, default=60, help='number of frames to render')
    parser.add_argument("--x_motion", type=float, default=0.20, help='x asix motion range')

    args = parser.parse_args()

    render(args)
