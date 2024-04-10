import os
from pathlib import Path

import comfy
import folder_paths
import torch
from comfy import model_management
from comfy.cli_args import args
from onediff.infer_compiler.utils import is_community_version

from ..modules.oneflow.config import ONEDIFF_QUANTIZED_OPTIMIZED_MODELS
from ..modules.oneflow.hijack_animatediff import animatediff_hijacker
from ..modules.oneflow.hijack_ipadapter_plus import ipadapter_plus_hijacker
from ..modules.oneflow.hijack_model_management import model_management_hijacker
from ..modules.oneflow.hijack_nodes import nodes_hijacker
from ..modules.oneflow.hijack_samplers import samplers_hijack
from ..modules.oneflow.optimizer_basic import BasicOneFlowOptimizerExecutor
from ..modules.oneflow.optimizer_deepcache import DeepcacheOptimizerExecutor
from ..modules.oneflow.optimizer_patch import PatchOptimizerExecutor
from ..modules.oneflow.optimizer_quantization import \
    OnelineQuantizationOptimizerExecutor
from ..modules.oneflow.utils import OUTPUT_FOLDER, load_graph, save_graph
from ..modules.optimizer_scheduler import OptimizerScheduler
from ..utils.import_utils import is_onediff_quant_available

model_management_hijacker.hijack()  # add flow.cuda.empty_cache()
nodes_hijacker.hijack()
samplers_hijack.hijack()
animatediff_hijacker.hijack()
ipadapter_plus_hijacker.hijack()

import comfy_extras.nodes_video_model
from nodes import CheckpointLoaderSimple

if not args.dont_upcast_attention:
    os.environ["ONEFLOW_ATTENTION_ALLOW_HALF_PRECISION_SCORE_ACCUMULATION_MAX_M"] = "0"


class OneFlowDeepcacheOptimizer:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "cache_interval": (
                    "INT",
                    {
                        "default": 3,
                        "min": 1,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "cache_layer_id": (
                    "INT",
                    {"default": 0, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "cache_block_id": (
                    "INT",
                    {"default": 1, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "start_step": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "end_step": (
                    "INT",
                    {"default": 1000, "min": 0, "max": 1000, "step": 0.1},
                ),
            },
        }

    CATEGORY = "OneDiff/Optimizer"
    RETURN_TYPES = ("DeepCacheOptimizer",)
    FUNCTION = "apply"

    @torch.no_grad()
    def apply(
        self,
        cache_interval=3,
        cache_layer_id=0,
        cache_block_id=1,
        start_step=0,
        end_step=1000,
    ):
        return (
            DeepcacheOptimizerExecutor(
                cache_interval=cache_interval,
                cache_layer_id=cache_layer_id,
                cache_block_id=cache_block_id,
                start_step=start_step,
                end_step=end_step,
            ),
        )

class ModuleDeepCacheSpeedup:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "static_mode": (["enable", "disable"],),
                "cache_interval": (
                    "INT",
                    {
                        "default": 3,
                        "min": 1,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "cache_layer_id": (
                    "INT",
                    {"default": 0, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "cache_block_id": (
                    "INT",
                    {"default": 1, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "start_step": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "end_step": (
                    "INT",
                    {"default": 1000, "min": 0, "max": 1000, "step": 0.1,},
                ),
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "deep_cache_convert"
    CATEGORY = "OneDiff"

    @torch.no_grad()
    def deep_cache_convert(
        self,
        model,
        static_mode,
        cache_interval,
        cache_layer_id,
        cache_block_id,
        start_step,
        end_step,
    ):
        op = OptimizerScheduler(DeepcacheOptimizerExecutor(cache_interval=cache_interval,
            cache_layer_id=cache_layer_id,
            cache_block_id=cache_block_id,
            start_step=start_step,
            end_step=end_step,))
        
        optimized_model = op.compile(model)
        return (optimized_model,)

class OneDiffOnlineQuantizationOptimizer:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "quantized_conv_percentage": (
                    "INT",
                    {
                        "default": 100,
                        "min": 0,  # Minimum value
                        "max": 100,  # Maximum value
                        "step": 1,  # Slider's step
                        "display": "slider",  # Cosmetic only: display as "number" or "slider"
                    },
                ),
                "quantized_linear_percentage": (
                    "INT",
                    {
                        "default": 100,
                        "min": 0,  # Minimum value
                        "max": 100,  # Maximum value
                        "step": 1,  # Slider's step
                        "display": "slider",  # Cosmetic only: display as "number" or "slider"
                    },
                ),
                "conv_compute_density_threshold":(
                   "INT",
                    {
                        "default": 100,
                        "min": 0,  # Minimum value
                        "max": 2000,  # Maximum value
                        "step": 10,  # Slider's step
                        "display": "number",  # Cosmetic only: display as "number" or "slider"
                    },
                ),
                "linear_compute_density_threshold":(
                    "INT",
                    {
                        "default": 300,
                        "min": 0,  # Minimum value
                        "max": 2000,  # Maximum value
                        "step": 10,  # Slider's step
                        "display": "number",  # Cosmetic only: display as "number" or "slider"
                    },
                )
            },
        }

    CATEGORY = "OneDiff/Optimizer"
    RETURN_TYPES = ("QuantizationOptimizer",)
    FUNCTION = "apply"

    @torch.no_grad()
    def apply(self, quantized_conv_percentage=0, quantized_linear_percentage=0, conv_compute_density_threshold=0, linear_compute_density_threshold=0):
        return (
            OnelineQuantizationOptimizerExecutor(
                conv_percentage=quantized_conv_percentage,
                linear_percentage=quantized_linear_percentage,
                conv_compute_density_threshold = conv_compute_density_threshold,
                linear_compute_density_threshold = linear_compute_density_threshold,
            ),
        )

class OneDiffDeepCacheCheckpointLoaderSimple(CheckpointLoaderSimple):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ckpt_name": (folder_paths.get_filename_list("checkpoints"),),
                "vae_speedup": (["disable", "enable"],),
                "static_mode": (["enable", "disable"],),
                "cache_interval": (
                    "INT",
                    {
                        "default": 3,
                        "min": 1,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "cache_layer_id": (
                    "INT",
                    {"default": 0, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "cache_block_id": (
                    "INT",
                    {"default": 1, "min": 0, "max": 12, "step": 1, "display": "number"},
                ),
                "start_step": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "step": 1,
                        "display": "number",
                    },
                ),
                "end_step": (
                    "INT",
                    {"default": 1000, "min": 0, "max": 1000, "step": 0.1,},
                ),
            }
        }

    CATEGORY = "OneDiff/Loaders"
    FUNCTION = "onediff_load_checkpoint"

    @torch.no_grad()
    def onediff_load_checkpoint(
        self,
        ckpt_name,
        vae_speedup,
        output_vae=True,
        output_clip=True,
        static_mode="enable",
        cache_interval=3,
        cache_layer_id=0,
        cache_block_id=1,
        start_step=0,
        end_step=1000,
    ):
        # CheckpointLoaderSimple.load_checkpoint
        modelpatcher, clip, vae = self.load_checkpoint(
            ckpt_name, output_vae, output_clip
        )
        op = OptimizerScheduler(DeepcacheOptimizerExecutor(
               cache_interval=cache_interval,
                cache_layer_id=cache_layer_id,
                cache_block_id=cache_block_id,
                start_step=start_step,
                end_step=end_step,
        ))

        modelpatcher = op.compile(modelpatcher, ckpt_name=ckpt_name)
        if vae_speedup == "enable":
            vae_op = OptimizerScheduler(BasicOneFlowOptimizerExecutor())
            vae = vae_op.compile(vae, ckpt_name=ckpt_name)

        # set inplace update
        modelpatcher.weight_inplace_update = True
        return modelpatcher, clip, vae

class BatchSizePatcher:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "latent_image": ("LATENT", ),
            },
        }

    RETURN_TYPES = ("MODEL",)
    CATEGORY = "OneDiff/Tools"
    FUNCTION = "set_cache_filename"

    @torch.no_grad()
    def set_cache_filename(self, model, latent_image):
        op = OptimizerScheduler(PatchOptimizerExecutor())
        model = op(model=model, latent_image=latent_image)
        return (model,)

class SVDSpeedup:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {"model": ("MODEL",), 
                        "inplace": ([False, True],),
                        "cache_name": ("STRING", {
                            "multiline": False, #True if you want the field to look like the one on the ClipTextEncode node
                            "default": "svd"}),
            },
            "optional": {
                "custom_optimizer": ("CUSTOM_OPTIMIZER",),
            }

        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "speedup"
    CATEGORY = "OneDiff"

    @torch.no_grad()
    def speedup(self, model, inplace=False, cache_name = "svd", custom_optimizer: OptimizerScheduler=None):
        if custom_optimizer:
            op = custom_optimizer
            op.inplace = inplace
        else:
            op = OptimizerScheduler(BasicOneFlowOptimizerExecutor(), inplace=inplace)

        optimized_model = op.compile(model, ckpt_name=cache_name)
        return (optimized_model,)

########################## For downward compatibility, it is retained ###################
class VaeGraphLoader:
    @classmethod
    def INPUT_TYPES(s):
        vae_folder = os.path.join(OUTPUT_FOLDER, "vae")
        graph_files = [
            f
            for f in os.listdir(vae_folder)
            if os.path.isfile(os.path.join(vae_folder, f)) and f.endswith(".graph")
        ]
        return {
            "required": {"vae": ("VAE",), "graph": (sorted(graph_files),),},
        }

    RETURN_TYPES = ("VAE",)
    FUNCTION = "load_graph"
    CATEGORY = "OneDiff"

    def load_graph(self, vae, graph):
        vae_model = vae.first_stage_model
        device = model_management.vae_offload_device()
        load_graph(vae_model, graph, device, subfolder="vae")
        return (vae,)

class VaeGraphSaver:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                "vae": ("VAE",),
                "filename_prefix": ("STRING", {"default": "vae"}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_graph"
    CATEGORY = "OneDiff"
    OUTPUT_NODE = True

    def save_graph(self, images, vae, filename_prefix):
        vae_model = vae.first_stage_model
        vae_device = model_management.vae_offload_device()
        save_graph(vae_model, filename_prefix, vae_device, subfolder="vae")

        return {}

class ModelGraphLoader:
    @classmethod
    def INPUT_TYPES(s):
        unet_folder = os.path.join(OUTPUT_FOLDER, "unet")
        graph_files = [
            f
            for f in os.listdir(unet_folder)
            if os.path.isfile(os.path.join(unet_folder, f)) and f.endswith(".graph")
        ]
        return {
            "required": {"model": ("MODEL",), "graph": (sorted(graph_files),),},
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_graph"
    CATEGORY = "OneDiff"

    def load_graph(self, model, graph):

        diffusion_model = model.model.diffusion_model

        load_graph(diffusion_model, graph, "cuda", subfolder="unet")
        return (model,)

class ModelGraphSaver:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "samples": ("LATENT",),
                "model": ("MODEL",),
                "filename_prefix": ("STRING", {"default": "unet"}),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_graph"
    CATEGORY = "OneDiff"
    OUTPUT_NODE = True

    def save_graph(self, samples, model, filename_prefix):
        diffusion_model = model.model.diffusion_model
        save_graph(diffusion_model, filename_prefix, "cuda", subfolder="unet")
        return {}

  
NODE_CLASS_MAPPINGS = {
   "ModelGraphLoader": ModelGraphLoader,
   "ModelGraphSaver": ModelGraphSaver,
   "VaeGraphSaver": VaeGraphSaver,
   "VaeGraphLoader": VaeGraphLoader,
   "ModuleDeepCacheSpeedup": ModuleDeepCacheSpeedup,
   "OneDiffDeepCacheCheckpointLoaderSimple": OneDiffDeepCacheCheckpointLoaderSimple,
   "BatchSizePatcher": BatchSizePatcher,
   "OneDiffOnlineQuantizationOptimizer": OneDiffOnlineQuantizationOptimizer,
   "SVDSpeedup": SVDSpeedup,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ModelGraphLoader": "Model Graph Loader",
    "ModelGraphSaver": "Model Graph Saver",
    "VaeGraphLoader": "VAE Graph Loader",
    "VaeGraphSaver": "VAE Graph Saver",
    "SVDSpeedup": "SVD Speedup",
    "ModuleDeepCacheSpeedup": "Model DeepCache Speedup",
    "OneDiffControlNetLoader": "Load ControlNet Model - OneDiff",
    "OneDiffDeepCacheCheckpointLoaderSimple": "Load Checkpoint - OneDiff DeepCache",
    "BatchSizePatcher": "Batch Size Patcher",
    "OneDiffOnlineQuantizationOptimizer": "Online OneFlow Quantizer - OneDiff"
}


if is_onediff_quant_available() and not is_community_version():
    class UNETLoaderInt8:
        @classmethod
        def INPUT_TYPES(cls):
            paths = []
            for search_path in folder_paths.get_folder_paths("unet_int8"):
                if os.path.exists(search_path):
                    for root, subdir, files in os.walk(search_path, followlinks=True):
                        if "calibrate_info.txt" in files:
                            paths.append(os.path.relpath(root, start=search_path))

            return {"required": {"model_path": (paths,),}}

        RETURN_TYPES = ("MODEL",)
        FUNCTION = "load_unet_int8"

        CATEGORY = "OneDiff"

        def load_unet_int8(self, model_path):
            from ..modules.oneflow.utils.onediff_quant_utils import \
                replace_module_with_quantizable_module
        

            for search_path in folder_paths.get_folder_paths("unet_int8"):
                if os.path.exists(search_path):
                    path = os.path.join(search_path, model_path)
                    if os.path.exists(path):
                        model_path = path
                        break

            unet_sd_path = os.path.join(model_path, "unet_int8.safetensors")
            calibrate_info_path = os.path.join(model_path, "calibrate_info.txt")

            model = comfy.sd.load_unet(unet_sd_path)
            replace_module_with_quantizable_module(
                model.model.diffusion_model, calibrate_info_path
            )
            return (model,)

    class Quant8Model:
        @classmethod
        def INPUT_TYPES(s):
            return {
                "required": {
                    "model": ("MODEL",),
                    "output_dir": ("STRING", {"default": "int8"}),
                    "conv": (["enable", "disable"],),
                    "linear": (["enable", "disable"],),
                },
            }

        RETURN_TYPES = ()
        FUNCTION = "quantize_model"
        CATEGORY = "OneDiff"
        OUTPUT_NODE = True

        def quantize_model(self, model, output_dir, conv, linear):
            from ..modules.oneflow.utils import quantize_and_save_model 

            diffusion_model = model.model.diffusion_model
            output_dir = os.path.join(folder_paths.models_dir, "unet_int8", output_dir)
            is_quantize_conv = conv == "enable"
            is_quantize_linear = linear == "enable"
            quantize_and_save_model(
                diffusion_model,
                output_dir,
                quantize_conv=is_quantize_conv,
                quantize_linear=is_quantize_linear,
                verbose=False,
            )
            return {}
    
    class OneDiffQuantCheckpointLoaderSimple(CheckpointLoaderSimple):
        @classmethod
        def INPUT_TYPES(s):
            return {
                "required": {
                    "ckpt_name": (folder_paths.get_filename_list("checkpoints"),),
                    "vae_speedup": (["disable", "enable"],),
                }
            }

        CATEGORY = "OneDiff/Loaders"
        FUNCTION = "onediff_load_checkpoint"

        def onediff_load_checkpoint(
            self, ckpt_name, vae_speedup, output_vae=True, output_clip=True
        ):
            modelpatcher, clip, vae = self.load_checkpoint(
                ckpt_name, output_vae, output_clip
            )
            op = OptimizerScheduler(OnelineQuantizationOptimizerExecutor(
                    conv_percentage=100,
                    linear_percentage=100,
                    conv_compute_density_threshold = 600,
                    linear_compute_density_threshold = 900,
                ))
            modelpatcher = op.compile(modelpatcher, ckpt_name=ckpt_name)
            if vae_speedup == "enable":
                vae_op = OptimizerScheduler(BasicOneFlowOptimizerExecutor())
                vae = vae_op.compile(vae, ckpt_name=ckpt_name)

            # set inplace update
            modelpatcher.weight_inplace_update = True
            return modelpatcher, clip, vae

    class OneDiffQuantCheckpointLoaderSimpleAdvanced(CheckpointLoaderSimple):
        @classmethod
        def INPUT_TYPES(s):
            paths = []
            for search_path in folder_paths.get_folder_paths(
                ONEDIFF_QUANTIZED_OPTIMIZED_MODELS
            ):
                if os.path.exists(search_path):
                    search_path = Path(search_path)
                    paths.extend(
                        [
                            os.path.relpath(p, start=search_path)
                            for p in search_path.glob("*.pt")
                        ]
                    )

            return {
                "required": {
                    "ckpt_name": (folder_paths.get_filename_list("checkpoints"),),
                    "model_path": (paths,),
                    "compile": (["enable", "disable"],),
                    "vae_speedup": (["disable", "enable"],),
                }
            }

        CATEGORY = "OneDiff/Loaders"
        FUNCTION = "onediff_load_checkpoint"

        def onediff_load_checkpoint(
            self,
            ckpt_name,
            model_path,
            compile,
            vae_speedup,
            output_vae=True,
            output_clip=True,
        ):
            need_compile = compile == "enable"

            modelpatcher, clip, vae = self.load_checkpoint(
                ckpt_name, output_vae, output_clip
            )
            # TODO fix by op.compile
            from ..modules.oneflow.utils.onediff_load_utils import \
                onediff_load_quant_checkpoint_advanced
            modelpatcher, vae = onediff_load_quant_checkpoint_advanced(
                ckpt_name, model_path, need_compile, vae_speedup, modelpatcher, vae
            )

            return modelpatcher, clip, vae
    
    class ImageOnlyOneDiffQuantCheckpointLoaderAdvanced(
        comfy_extras.nodes_video_model.ImageOnlyCheckpointLoader
    ):
        @classmethod
        def INPUT_TYPES(s):
            paths = []
            for search_path in folder_paths.get_folder_paths(
                ONEDIFF_QUANTIZED_OPTIMIZED_MODELS
            ):
                if os.path.exists(search_path):
                    search_path = Path(search_path)
                    paths.extend(
                        [
                            os.path.relpath(p, start=search_path)
                            for p in search_path.glob("*.pt")
                        ]
                    )

            return {
                "required": {
                    "ckpt_name": (folder_paths.get_filename_list("checkpoints"),),
                    "model_path": (paths,),
                    "compile": (["enable", "disable"],),
                    "vae_speedup": (["disable", "enable"],),
                }
            }

        CATEGORY = "OneDiff/Loaders"
        FUNCTION = "onediff_load_checkpoint"

        def onediff_load_checkpoint(
            self,
            ckpt_name,
            model_path,
            compile,
            vae_speedup,
            output_vae=True,
            output_clip=True,
        ):
            modelpatcher, clip, vae = self.load_checkpoint(
                ckpt_name, output_vae, output_clip
            )
            op = OptimizerScheduler(BasicOneFlowOptimizerExecutor())
            modelpatcher = op.compile(modelpatcher, ckpt_name=ckpt_name)
            if vae_speedup:
                vae_op = OptimizerScheduler(BasicOneFlowOptimizerExecutor())
                vae = vae_op.compile(vae, ckpt_name=ckpt_name)
            return modelpatcher, clip, vae

    NODE_CLASS_MAPPINGS.update(
        {
            "UNETLoaderInt8": UNETLoaderInt8,
            "Quant8Model": Quant8Model,
            "OneDiffQuantCheckpointLoaderSimple": OneDiffQuantCheckpointLoaderSimple,
            "OneDiffQuantCheckpointLoaderSimpleAdvanced": OneDiffQuantCheckpointLoaderSimpleAdvanced,
            "ImageOnlyOneDiffQuantCheckpointLoaderAdvanced": ImageOnlyOneDiffQuantCheckpointLoaderAdvanced,
        }
    )

    NODE_DISPLAY_NAME_MAPPINGS.update(
        {
            "UNETLoaderInt8": "UNET Loader Int8",
            "Quant8Model": "Model Quantization(int8)",
            "OneDiffQuantCheckpointLoaderSimple": "Load Checkpoint - OneDiff Quant",
            "OneDiffQuantCheckpointLoaderSimpleAdvanced": "Load Checkpoint - OneDiff Quant Advanced",
            "ImageOnlyOneDiffQuantCheckpointLoaderAdvanced": "Load Checkpoint - OneDiff Quant Advanced (img2vid)",
        }
    )