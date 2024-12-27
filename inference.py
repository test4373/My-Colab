# coding: utf-8
__author__ = 'Roman Solovyev (ZFTurbo): https://github.com/ZFTurbo/'

import argparse
import time
import librosa
from tqdm.auto import tqdm
import sys
import os
import glob
import torch
import soundfile as sf
import torch.nn as nn
from datetime import datetime

# Using the embedded version of Python can also correctly import the utils module.
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

from utils import demix, get_model_from_config, normalize_audio, denormalize_audio
from utils import prefer_target_instrument, apply_tta, load_start_checkpoint, load_lora_weights

import warnings
warnings.filterwarnings("ignore")


def run_folder(model, args, config, device, verbose: bool = False):
    start_time = time.time()
    model.eval()

    mixture_paths = sorted(glob.glob(os.path.join(args.input_folder, '*.*')))
    sample_rate = getattr(config.audio, 'sample_rate', 44100)

    print(f"Total files found: {len(mixture_paths)}. Using sample rate: {sample_rate}")

    instruments = prefer_target_instrument(config)[:]
    os.makedirs(args.store_dir, exist_ok=True)

    if args.disable_detailed_pbar:
        detailed_pbar = False
    else:
        detailed_pbar = True

    # Toplam dosya sayısı için tqdm
    progress_bar = tqdm(mixture_paths, desc="Total Progress", total=len(mixture_paths))

    for path in progress_bar:
        try:
            mix, sr = librosa.load(path, sr=sample_rate, mono=False)
        except Exception as e:
            print(f'Cannot read track: {format(path)}')
            print(f'Error message: {str(e)}')
            progress_bar.update(1)  # Hata olsa bile progress barı ilerlet
            continue

        mix_orig = mix.copy()
        if 'normalize' in config.inference:
            if config.inference['normalize'] is True:
                mix, norm_params = normalize_audio(mix)

        waveforms_orig = demix(config, model, mix, device, model_type=args.model_type, pbar=detailed_pbar)

        if args.use_tta:
            waveforms_orig = apply_tta(config, model, mix, waveforms_orig, device, args.model_type)

        if args.extract_instrumental:
            instr = 'vocals' if 'vocals' in instruments else instruments[0]
            waveforms_orig['instrumental'] = mix_orig - waveforms_orig[instr]
            if 'instrumental' not in instruments:
                instruments.append('instrumental')

        file_name = os.path.splitext(os.path.basename(path))[0]
        def shorten_filename(filename, max_length=30):
            """
            Dosya adını belirli bir uzunlukta kısaltır
            """
            base, ext = os.path.splitext(filename)
            if len(base) <= max_length:
                return filename
    
            # İlk 15 karakter ve son 10 karakteri al
            shortened = base[:15] + "..." + base[-10:] + ext
            return shortened

        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Model ismini clean_model değişkeninden al
        full_model_name = clean_model

        instrument_progress = tqdm(instruments, desc="Processing", leave=False)
        for instr in instrument_progress:
            estimates = waveforms_orig[instr]
            if 'normalize' in config.inference:
                if config.inference['normalize'] is True:
                    estimates = denormalize_audio(estimates, norm_params)

            codec = 'flac' if getattr(args, 'flac_file', False) else 'wav'
            ubtype = 'PCM_16' if args.flac_file and args.pcm_type == 'PCM_16' else 'FLOAT'

            # Orijinal dosya adını kısalt
            shortened_filename = shorten_filename(os.path.basename(path))

            # Model adı, kısaltılmış dosya adı, enstrüman ve zaman bilgisini içeren dosya ismi
            output_filename = f"{full_model_name}_{shortened_filename}_{instr}_{current_time}.{codec}"
            output_path = os.path.join(args.store_dir, output_filename)
    
            sf.write(output_path, estimates.T, sr, subtype=subtype)
    
            instrument_progress.set_postfix(instrument=instr)
    
            # Ana progress barı güncelle
            progress_bar.update(1)
            progress_bar.set_postfix(current_file=file_name)

    # Progress barı kapat
    progress_bar.close()

    print(f"Elapsed time: {time.time() - start_time:.2f} seconds.")

def proc_folder(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_type", type=str, default='mdx23c', help="One of bandit, bandit_v2, bs_roformer, htdemucs, mdx23c, mel_band_roformer, scnet, scnet_unofficial, segm_models, swin_upernet, torchseg")
    parser.add_argument("--config_path", type=str, help="path to config file")
    parser.add_argument("--start_check_point", type=str, default='', help="Initial checkpoint to valid weights")
    parser.add_argument("--input_folder", type=str, help="folder with mixtures to process")
    parser.add_argument("--store_dir", default="", type=str, help="path to store results as wav file")
    parser.add_argument("--device_ids", nargs='+', type=int, default=0, help='list of gpu ids')
    parser.add_argument("--extract_instrumental", action='store_true', help="invert vocals to get instrumental if provided")
    parser.add_argument("--disable_detailed_pbar", action='store_true', help="disable detailed progress bar")
    parser.add_argument("--force_cpu", action='store_true', help="Force the use of CPU even if CUDA is available")
    parser.add_argument("--flac_file", action='store_true', help="Output flac file instead of wav")
    parser.add_argument("--pcm_type", type=str, choices=['PCM_16', 'PCM_24'], default='PCM_24', help="PCM type for FLAC files (PCM_16 or PCM_24)")
    parser.add_argument("--use_tta", action='store_true', help="Flag adds test time augmentation during inference (polarity and channel inverse). While this triples the runtime, it reduces noise and slightly improves prediction quality.")
    parser.add_argument("--lora_checkpoint", type=str, default='', help="Initial checkpoint to LoRA weights")
    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    device = "cpu"
    if args.force_cpu:
        device = "cpu"
    elif torch.cuda.is_available():
        print('CUDA is available, use --force_cpu to disable it.')
        device = f'cuda:{args.device_ids[0]}' if type(args.device_ids) == list else f'cuda:{args.device_ids}'
    elif torch.backends.mps.is_available():
        device = "mps"

    print("Using device: ", device)

    model_load_start_time = time.time()
    torch.backends.cudnn.benchmark = True

    model, config = get_model_from_config(args.model_type, args.config_path)

    if args.start_check_point != '':
        load_start_checkpoint(args, model, type_='inference')

    print("Instruments: {}".format(config.training.instruments))

    # in case multiple CUDA GPUs are used and --device_ids arg is passed
    if type(args.device_ids) == list and len(args.device_ids) > 1 and not args.force_cpu:
        model = nn.DataParallel(model, device_ids = args.device_ids)

    model = model.to(device)

    print("Model load time: {:.2f} sec".format(time.time() - model_load_start_time))

    run_folder(model, args, config, device, verbose=True)


if __name__ == "__main__":
    proc_folder(None)