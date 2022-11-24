import inspect
import warnings
from typing import List, Optional, Union

import oneflow as torch

from transformers import CLIPFeatureExtractor, CLIPTokenizer
from transformers import OneFlowCLIPTextModel as CLIPTextModel

from ...configuration_utils import FrozenDict
from ...models import OneFlowAutoencoderKL as AutoencoderKL
from ...models import OneFlowUNet2DConditionModel as UNet2DConditionModel
from ...pipeline_oneflow_utils import OneFlowDiffusionPipeline as DiffusionPipeline
from ...schedulers import OneFlowDDIMScheduler as DDIMScheduler
from ...schedulers import OneFlowPNDMScheduler as PNDMScheduler
from ...schedulers import OneFlowDPMSolverMultistepScheduler as DPMSolverMultistepScheduler
from ...schedulers import LMSDiscreteScheduler
from . import StableDiffusionPipelineOutput
from .safety_checker_oneflow import OneFlowStableDiffusionSafetyChecker as StableDiffusionSafetyChecker
from timeit import default_timer as timer

import os

import oneflow as flow


class UNetGraph(flow.nn.Graph):
    def __init__(self, unet):
        super().__init__()
        self.unet = unet
        self.config.enable_cudnn_conv_heuristic_search_algo(False)
        # TODO: this now has negative impact on performance
        # self.config.allow_fused_add_to_output(True)

    def build(self, latent_model_input, t, text_embeddings):
        text_embeddings = torch._C.amp_white_identity(text_embeddings)
        return self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample


class UnrolledDenoiseGraph(flow.nn.Graph):
    def __init__(self, unet, scheduler, guidance_scale, extra_step_kwargs):
        super().__init__()
        self.unet = unet
        self.scheduler = scheduler
        self.extra_step_kwargs = extra_step_kwargs
        self.guidance_scale = guidance_scale

    def build(self, latents, text_embeddings):
        do_classifier_free_guidance = self.guidance_scale > 1.0
        extra_step_kwargs = self.extra_step_kwargs
        for i, t in enumerate(self.scheduler.timesteps):
            torch._oneflow_internal.profiler.RangePush(f"denoise_{i}")
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                sigma = self.scheduler.sigmas[i]
                # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
                latent_model_input = latent_model_input / ((sigma**2 + 1) ** 0.5)

            # predict the noise residual
            noise_pred_ = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings)
            noise_pred = noise_pred_.sample

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + self.guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            if isinstance(self.scheduler, LMSDiscreteScheduler):
                latents = self.scheduler.step(noise_pred, i, latents, **extra_step_kwargs).prev_sample
            else:
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample
            torch._oneflow_internal.profiler.RangePop()
        return latents


class VaePostProcess(flow.nn.Module):
    def __init__(self, vae) -> None:
        super().__init__()
        self.vae = vae

    def forward(self, x):
        x = 1 / 0.18215 * x
        return self.vae.decoder(self.vae.post_quant_conv(x))


class VaeGraph(flow.nn.Graph):
    def __init__(self, vae_post_process) -> None:
        super().__init__()
        self.vae_post_process = vae_post_process

    def build(self, latents):
        return self.vae_post_process(latents)


class TextEncoderGraph(flow.nn.Graph):
    def __init__(self, text_encoder) -> None:
        super().__init__()
        self.text_encoder = text_encoder

    def build(self, text_input):
        return self.text_encoder(text_input)[0]


class OneFlowStableDiffusionPipeline(DiffusionPipeline):
    r"""
    Pipeline for text-to-image generation using Stable Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder. Stable Diffusion uses the text portion of
            [CLIP](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), specifically
            the [clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14) variant.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class
            [CLIPTokenizer](https://huggingface.co/docs/transformers/v4.21.0/en/model_doc/clip#transformers.CLIPTokenizer).
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latens. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please, refer to the [model card](https://huggingface.co/CompVis/stable-diffusion-v1-4) for details.
        feature_extractor ([`CLIPFeatureExtractor`]):
            Model that extracts features from generated images to be used as inputs for the `safety_checker`.
    """

    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: Union[DDIMScheduler, PNDMScheduler, LMSDiscreteScheduler, DPMSolverMultistepScheduler],
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPFeatureExtractor,
    ):
        super().__init__()
        scheduler = scheduler.set_format("pt")

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            warnings.warn(
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file",
                DeprecationWarning,
            )
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
        )
        self.unet_graphs = dict()
        self.unet_graphs_cache_size = 1
        self.unet_graphs_lru_cache_time = 0
        self.vae_graph = None
        self.text_encoder_graph = None

    def enable_attention_slicing(self, slice_size: Optional[Union[str, int]] = "auto"):
        r"""
        Enable sliced attention computation.

        When this option is enabled, the attention module will split the input tensor in slices, to compute attention
        in several steps. This is useful to save some memory in exchange for a small speed decrease.

        Args:
            slice_size (`str` or `int`, *optional*, defaults to `"auto"`):
                When `"auto"`, halves the input to the attention heads, so attention will be computed in two steps. If
                a number is provided, uses as many slices as `attention_head_dim // slice_size`. In this case,
                `attention_head_dim` must be a multiple of `slice_size`.
        """
        if slice_size == "auto":
            # half the attention head size is usually a good trade-off between
            # speed and memory
            slice_size = self.unet.config.attention_head_dim // 2
        self.unet.set_attention_slice(slice_size)

    def disable_attention_slicing(self):
        r"""
        Disable sliced attention computation. If `enable_attention_slicing` was previously invoked, this method will go
        back to computing attention in one step.
        """
        # set slice_size = `None` to disable `attention slicing`
        self.enable_attention_slicing(None)

    def set_unet_graphs_cache_size(self, cache_size: int):
        r"""
        Set the cache size of compiled unet graphs.

        This option is designed to control the GPU memory size.

        Args:
            cache_size ([`int`]):
                New cache size, i.e., the maximum number of unet graphs.
        """
        self.unet_graphs_cache_size = cache_size

    @torch.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        eta: Optional[float] = 0.0,
        generator: Optional[torch.Generator] = None,
        latents: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        compile_text_encoder: bool = True,
        compile_unet: bool = True,
        compile_vae: bool = True,
        unrolled_timesteps: bool = False,
        **kwargs,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            height (`int`, *optional*, defaults to 512):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 512):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`torch.Generator`, *optional*):
                A [torch generator](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make generation
                deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            compile_unet (`bool`, *optional*, defaults to `True`):
                Whether or not to compile unet as nn.graph
            unrolled_timesteps (`bool`, *optional*, defaults to `False`):
                Whether or not to unroll the timesteps

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """
        os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
        os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
        os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"
        os.environ["ONEFLOW_KERNEL_ENABLE_CUDNN_FUSED_CONV_BIAS"] = "1"
        os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"
        os.environ["ONEFLOW_MLIR_GROUP_MATMUL"] = "1"
        os.environ["ONEFLOW_MLIR_CSE"] = "1"
        start = timer()
        if "torch_device" in kwargs:
            device = kwargs.pop("torch_device")
            warnings.warn(
                "`torch_device` is deprecated as an input argument to `__call__` and will be removed in v0.3.0."
                " Consider using `pipe.to(torch_device)` instead."
            )

            # Set device as before (to be removed in 0.3.0)
            if device is None:
                device = "cuda" if torch.cuda.is_available() else "cpu"
            self.to(device)

        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        # get prompt text embeddings
        text_input = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="np",
        )
        text_input.input_ids = torch.from_numpy(text_input.input_ids)
        if compile_text_encoder and self.text_encoder_graph is None:
            self.text_encoder_graph = TextEncoderGraph(self.text_encoder)
        torch._oneflow_internal.profiler.RangePush(f"text-encoder")
        _input_ids = text_input.input_ids.to(self.device)
        if compile_text_encoder:
            text_embeddings = self.text_encoder_graph(_input_ids)
        else:
            text_embeddings = self.text_encoder(_input_ids)[0]

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            max_length = text_input.input_ids.shape[-1]
            uncond_input = self.tokenizer(
                [""] * batch_size, padding="max_length", max_length=max_length, return_tensors="np"
            )
            uncond_input.input_ids = torch.from_numpy(uncond_input.input_ids)
            uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])
        torch._oneflow_internal.profiler.RangePop()
        # get the initial random noise unless the user supplied it

        # Unlike in other pipelines, latents need to be generated in the target device
        # for 1-to-1 results reproducibility with the CompVis implementation.
        # However this currently doesn't work in `mps`.
        latents_device = "cpu" if self.device.type == "mps" else self.device
        latents_shape = (batch_size, self.unet.in_channels, height // 8, width // 8)
        if latents is None:
            latents = torch.randn(
                latents_shape,
                generator=generator,
                device=latents_device,
            )
        else:
            if latents.shape != latents_shape:
                raise ValueError(f"Unexpected latents shape, got {latents.shape}, expected {latents_shape}")
        latents = latents.to(self.device)

        # set timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        # if we use LMSDiscreteScheduler, let's make sure latents are multiplied by sigmas
        if isinstance(self.scheduler, LMSDiscreteScheduler):
            latents = latents * self.scheduler.sigmas[0]

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        compilation_start = timer()
        compilation_time = 0

        if compile_unet:
            self.unet_graphs_lru_cache_time += 1
            if (height, width) in self.unet_graphs:
                _, unet_graph = self.unet_graphs[height, width]
                unrolled_timesteps_graph = unet_graph
                self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unet_graph)
            else:
                while len(self.unet_graphs) >= self.unet_graphs_cache_size:
                    shape_to_del = min(self.unet_graphs.keys(), key=lambda shape: self.unet_graphs[shape][0])
                    print(
                        "[oneflow]",
                        f"a compiled unet (height={shape_to_del[0]}, width={shape_to_del[1]}) "
                        "is deleted according to the LRU policy",
                    )
                    print("[oneflow]", "cache size can be changed by `pipeline.set_unet_graphs_cache_size`")
                    del self.unet_graphs[shape_to_del]
                print("[oneflow]", "compiling unet beforehand to make sure the progress bar is more accurate")

                if unrolled_timesteps:
                    unrolled_timesteps_graph = UnrolledDenoiseGraph(
                        self.unet, self.scheduler, guidance_scale, extra_step_kwargs
                    )
                    unrolled_timesteps_graph._compile(latents, text_embeddings)
                    unrolled_timesteps_graph(latents, text_embeddings)  # warmup
                    self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unrolled_timesteps_graph)
                else:
                    i, t = list(enumerate(self.scheduler.timesteps))[0]
                    latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                    unet_graph = UNetGraph(self.unet)
                    unet_graph._compile(latent_model_input, t, text_embeddings)
                    unet_graph(latent_model_input, t, text_embeddings)  # warmup
                    self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unet_graph)

                compilation_time = timer() - compilation_start
                print("[oneflow]", "[elapsed(s)]", "[unet compilation]", compilation_time)
        if compile_vae and (self.vae_graph is None):
            vae_post_process = VaePostProcess(self.vae)
            vae_post_process.eval()
            self.vae_graph = VaeGraph(vae_post_process)
        torch.cuda.synchronize()
        denoise_start = timer()
        if unrolled_timesteps:
            latents = unrolled_timesteps_graph(latents, text_embeddings)
        else:
            for i, t in enumerate(self.progress_bar(self.scheduler.timesteps)):
                torch._oneflow_internal.profiler.RangePush(f"denoise-{i}")
                # expand the latents if we are doing classifier free guidance
                latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                if isinstance(self.scheduler, LMSDiscreteScheduler):
                    sigma = self.scheduler.sigmas[i]
                    # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
                    latent_model_input = latent_model_input / ((sigma**2 + 1) ** 0.5)

                # predict the noise residual
                if compile_unet:
                    torch._oneflow_internal.profiler.RangePush(f"denoise-{i}-unet-graph")
                    noise_pred = unet_graph(latent_model_input, t, text_embeddings)
                    torch._oneflow_internal.profiler.RangePop()
                else:
                    noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                    noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                # compute the previous noisy sample x_t -> x_t-1
                if isinstance(self.scheduler, LMSDiscreteScheduler):
                    latents = self.scheduler.step(noise_pred, i, latents, **extra_step_kwargs).prev_sample
                else:
                    latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample
                torch._oneflow_internal.profiler.RangePop()
        torch.cuda.synchronize()
        dur_denoise = timer() - denoise_start
        print("[oneflow]", "[elapsed(s)]", "[denoise]", dur_denoise)
        print(
            "[oneflow]",
            "[denoise]",
            f"[{len(self.scheduler.timesteps)} steps]",
            float("{:.2f}".format(1 / (dur_denoise / len(self.scheduler.timesteps)))),
            "it/s",
        )
        if compile_vae:
            image = self.vae_graph(latents)
        else:
            # scale and decode the image latents with vae
            latents = 1 / 0.18215 * latents
            import numpy as np

            if isinstance(latents, np.ndarray):
                latents = torch.from_numpy(latents)

            image = self.vae.decode(latents).sample
        print("[oneflow]", "[elapsed(s)]", "[image]", timer() - start - compilation_time)
        post_process_start = timer()

        image = (image / 2 + 0.5).clamp(0, 1)
        image = image.cpu().permute(0, 2, 3, 1).numpy()

        # run safety checker
        safety_checker_input = self.feature_extractor(self.numpy_to_pil(image), return_tensors="np")
        safety_checker_input.pixel_values = torch.from_numpy(safety_checker_input.pixel_values).to(self.device)
        torch._oneflow_internal.profiler.RangePush(f"safety-checker")
        image, has_nsfw_concept = self.safety_checker(images=image, clip_input=safety_checker_input.pixel_values)
        torch._oneflow_internal.profiler.RangePop()

        if output_type == "pil":
            image = self.numpy_to_pil(image)

        if not return_dict:
            return (image, has_nsfw_concept)
        import torch as og_torch

        assert og_torch.cuda.is_initialized() is False

        print("[oneflow]", "[elapsed(s)]", "[post-process]", timer() - post_process_start)
        return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)

    # @torch.no_grad()
    # def graph_forward(
    #     self,
    #     prompt: Union[str, List[str]],
    #     height: Optional[int] = 512,
    #     width: Optional[int] = 512,
    #     num_inference_steps: Optional[int] = 50,
    #     guidance_scale: Optional[float] = 7.5,
    #     eta: Optional[float] = 0.0,
    #     generator: Optional[torch.Generator] = None,
    #     latents: Optional[torch.FloatTensor] = None,
    #     output_type: Optional[str] = "pil",
    # ):
    #     os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
    #     os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
    #     os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"
    #     os.environ["ONEFLOW_KERNEL_ENABLE_CUDNN_FUSED_CONV_BIAS"] = "1"
    #     os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"
    #     os.environ["ONEFLOW_MLIR_GROUP_MATMUL"] = "1"
    #     os.environ["ONEFLOW_MLIR_CSE"] = "1"

    #     if isinstance(prompt, str):
    #         batch_size = 1
    #     elif isinstance(prompt, list):
    #         batch_size = len(prompt)
    #     else:
    #         raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

    #     if height % 8 != 0 or width % 8 != 0:
    #         raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

    #     # get prompt text embeddings
    #     text_input = self.tokenizer(
    #         prompt,
    #         padding="max_length",
    #         max_length=self.tokenizer.model_max_length,
    #         truncation=True,
    #         return_tensors="np",
    #     )
    #     text_input.input_ids = torch.from_numpy(text_input.input_ids)
    #     self.text_encoder_graph = TextEncoderGraph(self.text_encoder)
    #     _input_ids = text_input.input_ids.to(self.device)
    #     text_embeddings = self.text_encoder_graph(_input_ids)

    #     do_classifier_free_guidance = guidance_scale > 1.0
    #     # get unconditional embeddings for classifier free guidance
    #     if do_classifier_free_guidance:
    #         max_length = text_input.input_ids.shape[-1]
    #         uncond_input = self.tokenizer(
    #             [""] * batch_size, padding="max_length", max_length=max_length, return_tensors="np"
    #         )
    #         uncond_input.input_ids = torch.from_numpy(uncond_input.input_ids)
    #         uncond_embeddings = self.text_encoder(uncond_input.input_ids.to(self.device))[0]

    #         # For classifier free guidance, we need to do two forward passes.
    #         # Here we concatenate the unconditional and text embeddings into a single batch
    #         # to avoid doing two forward passes
    #         text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

    #     latents_device = "cpu" if self.device.type == "mps" else self.device
    #     latents_shape = (batch_size, self.unet.in_channels, height // 8, width // 8)

    #     if latents is None:
    #         latents = torch.randn(
    #             latents_shape,
    #             generator=generator,
    #             device=latents_device,
    #         )
    #     latents = latents.to(self.device)

    #     self.scheduler.set_timesteps(num_inference_steps)
    #     if isinstance(self.scheduler, LMSDiscreteScheduler):
    #         latents = latents * self.scheduler.sigmas[0]

    #     accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
    #     extra_step_kwargs = {}
    #     if accepts_eta:
    #         extra_step_kwargs["eta"] = eta

    #     self.unet_graphs_lru_cache_time += 1
    #     if (height, width) in self.unet_graphs:
    #         _, unet_graph = self.unet_graphs[height, width]
    #         unrolled_timesteps_graph = unet_graph
    #         self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unet_graph)
    #     else:
    #         while len(self.unet_graphs) >= self.unet_graphs_cache_size:
    #             shape_to_del = min(self.unet_graphs.keys(), key=lambda shape: self.unet_graphs[shape][0])
    #             print(
    #                 "[oneflow]",
    #                 f"a compiled unet (height={shape_to_del[0]}, width={shape_to_del[1]}) "
    #                 "is deleted according to the LRU policy",
    #             )
    #             print("[oneflow]", "cache size can be changed by `pipeline.set_unet_graphs_cache_size`")
    #             del self.unet_graphs[shape_to_del]
    #         print("[oneflow]", "compiling unet beforehand to make sure the progress bar is more accurate")

    #         unrolled_timesteps_graph = UnrolledDenoiseGraph(
    #             self.unet, self.scheduler, guidance_scale, extra_step_kwargs
    #         )
    #         print(latents.shape)
    #         print("\n")
    #         print(text_embeddings.shape)
    #         print("\n")
    #         print(extra_step_kwargs)
    #         unrolled_timesteps_graph._compile(latents, text_embeddings)
    #         unrolled_timesteps_graph(latents, text_embeddings)  # warmup
    #         self.unet_graphs[height, width] = (self.unet_graphs_lru_cache_time, unrolled_timesteps_graph)

    #     latents = unrolled_timesteps_graph(latents, text_embeddings)

    #     vae_graph = VaeGraph(VaePostProcess(self.vae))

    #     image = vae_graph(latents)

    #     image = (image / 2 + 0.5).clamp(0, 1)
    #     image = image.cpu().permute(0, 2, 3, 1).numpy()

    #     if output_type == "pil":
    #         image = self.numpy_to_pil(image)
    #     return image
