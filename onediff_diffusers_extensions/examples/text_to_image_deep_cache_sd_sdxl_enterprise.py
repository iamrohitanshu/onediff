import argparse
import os
import time

import torch
import torch.nn as nn

# oneflow_compile should be imported before importing any diffusers
from onediff.infer_compiler import oneflow_compile, CompileOptions


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--model", type=str, required=True)
    parser.add_argument(
        "--model_type",
        type=str,
        required=True,
        choices=["sd", "sdxl"],
        help="Specify the model type: 'sd' for Stable Diffusion or 'sdxl' for Stable Diffusion XL.",
    )
    parser.add_argument("--saved_image", type=str, required=True)
    parser.add_argument("--save_graph", action="store_true")
    parser.add_argument("--load_graph", action="store_true")
    parser.add_argument(
        "--prompt",
        type=str,
        default="photo of a rhino dressed suit and tie sitting at a table in a bar with a bar stools, award winning photography, Elke vogelsang",
    )
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--bits", type=int, default=8)
    parser.add_argument(
        "--compile",
        default=True,
        type=(lambda x: str(x).lower() in ["true", "1", "yes"]),
    )
    parser.add_argument(
        "--compile_text_encoder",
        default=False,
        type=(lambda x: str(x).lower() in ["true", "1", "yes"]),
        help=(
            "Switch controls whether text_encoder is compiled (default: False). "
            "If your CPU is powerful, turning it on will shorten end-to-end time."
        ),
    )
    parser.add_argument(
        "--graph",
        default=True,
        type=(lambda x: str(x).lower() in ["true", "1", "yes"]),
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--cache_interval", type=int, default=3)
    parser.add_argument("--cache_layer_id", type=int, default=0)
    parser.add_argument("--cache_block_id", type=int, default=0)

    args = parser.parse_args()
    return args


args = parse_args()

assert os.path.isfile(
    os.path.join(args.model, "calibrate_info.txt")
), f"calibrate_info.txt is required in args.model ({args.model})"

if args.model_type == "sdxl":
    from onediffx.deep_cache import StableDiffusionXLPipeline
else:
    from onediffx.deep_cache import StableDiffusionPipeline
import onediff_quant
from onediff_quant.utils import replace_sub_module_with_quantizable_module

onediff_quant.enable_load_quantized_model()
infer_args = {
    "prompt": args.prompt,
    "height": args.height,
    "width": args.width,
    "num_inference_steps": args.steps,
    "cache_interval": args.cache_interval,
    "cache_layer_id": args.cache_layer_id,
    "cache_block_id": args.cache_block_id,
}
calibrate_info = {}
with open(os.path.join(args.model, "calibrate_info.txt"), "r") as f:
    for line in f.readlines():
        line = line.strip()
        items = line.split(" ")
        calibrate_info[items[0]] = [
            float(items[1]),
            int(items[2]),
            [float(x) for x in items[3].split(",")],
        ]

if args.model_type == "sdxl":
    pipe = StableDiffusionXLPipeline.from_pretrained(
        args.model, torch_dtype=torch.float16, use_safetensors=True, variant="fp16",
    )
else:
    pipe = StableDiffusionPipeline.from_pretrained(
        args.model, revision="fp16", variant="fp16", torch_dtype=torch.float16,
    )
pipe.to("cuda")

for sub_module_name, sub_calibrate_info in calibrate_info.items():
    replace_sub_module_with_quantizable_module(
        pipe.unet, sub_module_name, sub_calibrate_info, False, False, args.bits,
    )

options = CompileOptions()
options.oneflow.use_graph = args.graph

if args.compile_text_encoder:
    if pipe.text_encoder is not None:
        pipe.text_encoder = oneflow_compile(pipe.text_encoder, options=options)
    if hasattr(pipe, "text_encoder_2"):
        pipe.text_encoder_2 = oneflow_compile(pipe.text_encoder_2, options=options)

if args.compile:
    if pipe.text_encoder is not None:
        pipe.text_encoder = oneflow_compile(pipe.text_encoder, options=options)
    if hasattr(pipe, "text_encoder_2"):
        pipe.text_encoder_2 = oneflow_compile(pipe.text_encoder_2, options=options)
    pipe.unet = oneflow_compile(pipe.unet, options=options)
    pipe.fast_unet = oneflow_compile(pipe.fast_unet, options=options)
    if hasattr(pipe, "text_encoder_2") and pipe.needs_upcasting:
        # To avoid mis-match of loaded graph and loaded model
        pipe.upcast_vae()
    pipe.vae.decoder = oneflow_compile(pipe.vae.decoder, options=options)

if args.load_graph:
    print("Loading graphs to avoid compilation...")
    start_t = time.time()
    pipe.unet.load_graph("base_unet_compiled", run_warmup=True)
    pipe.fast_unet.load_graph("base_fast_unet_compiled", run_warmup=True)
    pipe.vae.decoder.load_graph("base_vae_compiled", run_warmup=True)
    end_t = time.time()
    print(f"warmup with loading graph elapsed: {end_t - start_t} s")
    start_t = time.time()
    for _ in range(args.warmup):
        image = pipe(**infer_args).images[0]
    end_t = time.time()
    print(f"warmup with run elapsed: {end_t - start_t} s")
else:
    start_t = time.time()
    for _ in range(args.warmup):
        image = pipe(**infer_args).images[0]
    end_t = time.time()
    print(f"warmup with run elapsed: {end_t - start_t} s")

torch.manual_seed(args.seed)

start_t = time.time()

torch.cuda.cudart().cudaProfilerStart()
image = pipe(**infer_args).images[0]
torch.cuda.cudart().cudaProfilerStop()

end_t = time.time()
print(f"e2e ({args.steps} steps) elapsed: {end_t - start_t} s")

image.save(args.saved_image)

if args.save_graph:
    print("Saving graphs...")
    start_t = time.time()
    pipe.unet.save_graph("base_unet_compiled")
    pipe.fast_unet.save_graph("base_fast_unet_compiled")
    pipe.vae.decoder.save_graph("base_vae_compiled")
    end_t = time.time()
    print(f"save graphs elapsed: {end_t - start_t} s")
