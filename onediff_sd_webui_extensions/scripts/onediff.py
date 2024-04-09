import os
import re
import warnings
import gradio as gr
from pathlib import Path
from typing import Union, Dict
from collections import defaultdict
import oneflow as flow
import modules.scripts as scripts
import modules.shared as shared
from modules.sd_models import select_checkpoint
from modules.processing import process_images
from modules import script_callbacks

from compile_ldm import compile_ldm_unet, SD21CompileCtx
from compile_sgm import compile_sgm_unet
from compile_vae import VaeCompileCtx
from onediff_lora import HijackLoraActivate
from onediff_hijack import do_hijack as onediff_do_hijack

from onediff.infer_compiler.utils.log_utils import logger
from onediff.infer_compiler.utils.env_var import parse_boolean_from_env
from onediff.optimization.quant_optimizer import (
    quantize_model,
    varify_can_use_quantization,
)
from onediff import __version__ as onediff_version
from oneflow import __version__ as oneflow_version

"""oneflow_compiled UNetModel"""
compiled_unet = None
compiled_ckpt_name = None


def generate_graph_path(ckpt_name: str, model_name: str) -> str:
    base_output_dir = shared.opts.outdir_samples or shared.opts.outdir_txt2img_samples
    save_ckpt_graphs_path = os.path.join(base_output_dir, "graphs", ckpt_name)
    os.makedirs(save_ckpt_graphs_path, exist_ok=True)

    file_name = f"{model_name}_graph_{onediff_version}_oneflow_{oneflow_version}"

    graph_file_path = os.path.join(save_ckpt_graphs_path, file_name)

    return graph_file_path


def get_calibrate_info(filename: str) -> Union[None, Dict]:
    calibration_path = Path(select_checkpoint().filename).parent / filename
    if not calibration_path.exists():
        return None

    logger.info(f"Got calibrate info at {str(calibration_path)}")
    calibrate_info = {}
    with open(calibration_path, "r") as f:
        for line in f.readlines():
            line = line.strip()
            items = line.split(" ")
            calibrate_info[items[0]] = [
                float(items[1]),
                int(items[2]),
                [float(x) for x in items[3].split(",")],
            ]
    return calibrate_info


def compile_unet(
    unet_model, quantization=False, *, options=None,
):
    from ldm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelLDM
    from sgm.modules.diffusionmodules.openaimodel import UNetModel as UNetModelSGM

    if isinstance(unet_model, UNetModelLDM):
        compiled_unet = compile_ldm_unet(unet_model, options=options)
    elif isinstance(unet_model, UNetModelSGM):
        compiled_unet = compile_sgm_unet(unet_model, options=options)
    else:
        warnings.warn(
            f"Unsupported model type: {type(unet_model)} for compilation , skip",
            RuntimeWarning,
        )
        compiled_unet = unet_model
    if quantization:
        calibrate_info = get_calibrate_info(
            f"{Path(select_checkpoint().filename).stem}_sd_calibrate_info.txt"
        )
        compiled_unet = quantize_model(
            compiled_unet, inplace=False, calibrate_info=calibrate_info
        )
    return compiled_unet


class UnetCompileCtx(object):
    """The unet model is stored in a global variable.
    The global variables need to be replaced with compiled_unet before process_images is run,
    and then the original model restored so that subsequent reasoning with onediff disabled meets expectations.
    """

    def __enter__(self):
        self._original_model = shared.sd_model.model.diffusion_model
        global compiled_unet
        shared.sd_model.model.diffusion_model = compiled_unet

    def __exit__(self, exc_type, exc_val, exc_tb):
        shared.sd_model.model.diffusion_model = self._original_model
        return False


class Script(scripts.Script):
    current_type = None
    convname_dict = None

    def title(self):
        return "onediff_diffusion_model"

    def ui(self, is_img2img):
        """this function should create gradio UI elements. See https://gradio.app/docs/#components
        The return value should be an array of all components that are used in processing.
        Values of those returned components will be passed to run() and process() functions.
        """
        if not varify_can_use_quantization():
            ret = gr.HTML(
                """
                    <div style="padding: 20px; border: 1px solid #e0e0e0; border-radius: 5px; background-color: #f9f9f9;">
                        <div style="font-size: 18px; font-weight: bold; margin-bottom: 15px; color: #31708f;">
                            Hints Message
                        </div>
                        <div style="padding: 10px; border: 1px solid #31708f; border-radius: 5px; background-color: #f9f9f9;">
                            Hints: Enterprise function is not supported on your system.
                        </div>
                        <p style="margin-top: 15px;">
                            If you need Enterprise Level Support for your system or business, please send an email to 
                            <a href="mailto:business@siliconflow.com" style="color: #31708f; text-decoration: none;">business@siliconflow.com</a>.
                            <br>
                            Tell us about your use case, deployment scale, and requirements.
                        </p>
                        <p>
                            <strong>GitHub Issue:</strong>
                            <a href="https://github.com/siliconflow/onediff/issues" style="color: #31708f; text-decoration: none;">https://github.com/siliconflow/onediff/issues</a>
                        </p>
                    </div>
                    """
            )

        else:
            ret = gr.components.Checkbox(label="Model Quantization(int8) Speed Up")
        return [ret]

    def show(self, is_img2img):
        return True

    def check_model_structure_change(self, model):
        is_changed = False

        def get_model_type(model):
            return {
                "is_sdxl": model.is_sdxl,
                "is_sd2": model.is_sd2,
                "is_sd1": model.is_sd1,
                "is_ssd": model.is_ssd,
            }

        if self.current_type == None:
            is_changed = True
        else:
            for key, v in self.current_type.items():
                if v != getattr(model, key):
                    is_changed = True
                    break

        if is_changed == True:
            self.current_type = get_model_type(model)
        return is_changed

    def run(self, p, quantization=False):
        # For OneDiff Community, the input param `quantization` is a HTML string
        if isinstance(quantization, str):
            quantization = False

        global compiled_unet, compiled_ckpt_name
        current_checkpoint = shared.opts.sd_model_checkpoint
        original_diffusion_model = shared.sd_model.model.diffusion_model

        ckpt_name = (
            current_checkpoint + "_quantized" if quantization else current_checkpoint
        )

        model_changed = ckpt_name != compiled_ckpt_name
        model_structure_changed = self.check_model_structure_change(shared.sd_model)
        need_recompile = (quantization and model_changed) or model_structure_changed
        if not need_recompile:
            logger.info(
                f"Model {current_checkpoint} has same sd type of graph type {self.current_type}, skip compile"
            )
            if model_changed:
                # need to transpose conv weights
                for k in self.convname_dict:
                    orig_tensor = original_diffusion_model.get_parameter(k)
                    target_tensor = self.convname_dict[k]
                    if target_tensor is None:
                        need_recompile = True
                        break
                    target_tensor.copy_(
                        flow.utils.tensor.from_torch(orig_tensor.permute(0, 2, 3, 1))
                    )

        if need_recompile:
            compiled_unet = compile_unet(
                original_diffusion_model, quantization=quantization
            )
            compiled_ckpt_name = ckpt_name
            self.convname_dict = None

        with UnetCompileCtx(), VaeCompileCtx(), SD21CompileCtx(), HijackLoraActivate(
            self.convname_dict
        ):
            proc = process_images(p)

        # AutoNHWC will transpose conv weight, which generate a new tensor in graph
        # The part is to find the corresponding relationship between the tensors before/after transpose
        def convert_var_name(s: str, prefix="variable_transpose_"):
            s = re.sub(r"_[0-9]+$", "", s.removeprefix(prefix)).removeprefix("model.")
            return s

        if not quantization and self.convname_dict is None:
            self.convname_dict = {}
            run_state = (
                compiled_unet._deployable_module_dpl_graph._c_nn_graph.get_runtime_var_states()
            )
            self.convname_dict = {
                convert_var_name(k): v
                for k, v in zip(run_state[0], run_state[1])
                if k.startswith("variable_")
            }
        return proc


onediff_do_hijack()
