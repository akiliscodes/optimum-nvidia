#  coding=utf-8
#  Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#  #
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  #
#      http://www.apache.org/licenses/LICENSE-2.0
#  #
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import json
import os
from logging import getLogger
from multiprocessing import Pool
from os import PathLike, sched_getaffinity
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Type, Union

import torch
from huggingface_hub import CONFIG_NAME, ModelHubMixin
from huggingface_hub.hub_mixin import T
from psutil import virtual_memory
from tensorrt_llm import Mapping as Shard
from tensorrt_llm import graph_rewriting
from tensorrt_llm.builder import Builder
from tensorrt_llm.models import LLaMAForCausalLM  # , TaurusForCausalLM
from tensorrt_llm.network import net_guard
from tensorrt_llm.plugin.plugin import ContextFMHAType
from tensorrt_llm.quantization import QuantMode
from transformers import AutoModelForCausalLM

from optimum.nvidia import DataType
from optimum.nvidia.errors import UnsupportedHardwareFeature
from optimum.nvidia.models import SupportsFromHuggingFace
from optimum.nvidia.quantization import Calibration
from optimum.nvidia.utils import (
    OPTIMUM_NVIDIA_CONFIG_FILE,
    TENSORRT_TIMINGS_FILE,
    maybe_offload_weights_to_cpu,
    parse_flag_from_env,
)
from optimum.nvidia.utils.nvml import get_device_count, get_device_memory


LOGGER = getLogger(__name__)


# Maps from model type to TRTLLM conversion scripts
_MODEL_TYPE_TO_TRT_IMPL: Mapping[str, SupportsFromHuggingFace] = {
    "llama": LLaMAForCausalLM,
    "mistral": LLaMAForCausalLM,
    # "gemma": TaurusForCausalLM
}


# Utility classes to store build information
BuildInfo = NamedTuple(
    "BuildInfo",
    [
        ("parallel", bool),
        ("num_parallel_jobs", int),
        ("quantized_path", Optional[Path]),
    ],
)
SERIAL_BUILD = BuildInfo(False, 1, None)

# Utility classes to store shape information
OptimizationProfile = NamedTuple(
    "OptimizationProfile",
    [
        ("max_batch_size", int),
        ("max_prompt_length", int),
        ("max_new_tokens", int),
        ("max_output_length", int),
    ],
)


# Utility classes to store sharding information
ShardingInfo = NamedTuple(
    "ShardingInfo",
    [
        ("tp_degree", int),
        ("pp_degree", int),
        ("world_size", int),
        ("num_gpus_per_node", int),
    ],
)
NO_SHARDING = ShardingInfo(1, 1, 1, 1)


def create_unique_engine_name(
    identifier: str, dtype: str, rank: int, tp_degree: int
) -> str:
    return f"{identifier}_{dtype}_tp{tp_degree}_rank{rank}.engine"


def create_npz_calibration_filename(identifier: str, rank: int, tp_degree: int) -> str:
    return f"{identifier}_tp{tp_degree}_rank{rank}.npz"


class TensorRTEngineBuilder(ModelHubMixin):
    """ """

    # Keeping AutoModelForCausalLM in this base class for backward compatibility with Llama.
    LOADING_CLASS = AutoModelForCausalLM

    @classmethod
    def _from_pretrained(
        cls: Type[T],
        *,
        model_id: str,
        revision: Optional[str],
        cache_dir: Optional[Union[str, Path]],
        force_download: bool,
        proxies: Optional[Dict],
        resume_download: bool,
        local_files_only: bool,
        token: Optional[Union[str, bool]],
        **model_kwargs,
    ) -> T:
        config = model_kwargs.get("config", None)  # TODO: Ensure this is ok
        return cls(model_id, config)

    def __init__(
        self, model_id_or_path: Union[str, PathLike], config: Mapping[str, Any]
    ):
        # Model
        # TODO: passing the adapter here should be optional - we can find it out simply from the model_id_or_path config model_type.
        self._model_id_or_path: Union[str, PathLike] = model_id_or_path
        self._model_config = config

        # Engine build
        self._dtype = self._model_config["torch_dtype"]
        self._build_info: BuildInfo = SERIAL_BUILD
        self._sharding_info: ShardingInfo = NO_SHARDING
        self._optimization_profile: Optional[OptimizationProfile] = None

        # Quantization
        self._qconfig: Optional[QuantMode] = None
        self._quantization_calibration: Optional[Calibration] = None

        # Sampling
        self._beam_width = -1

    def enable_parallel_build(self, num_jobs: int = -1) -> "TensorRTEngineBuilder":
        """

        :param num_jobs:
        :return:
        """
        # if self._build_info:
        #     raise Exception(f"Cannot specify twice building info ({self._build_info}).")

        LOGGER.debug(
            f"Setting parallel build strategy to use a maximum of {num_jobs} parallel jobs"
        )
        self._build_info = BuildInfo(True, num_jobs)

        return self

    def to(self, dtype: DataType) -> "TensorRTEngineBuilder":
        """

        :param dtype:
        :return:
        """
        if dtype.value != self._dtype:
            LOGGER.debug(f"Setting target dtype to {dtype}")
            self._dtype = dtype.value

        return self

    def shard(
        self, tp_degree: int, pp_degree: int, world_size: int, num_gpus_per_node: int
    ) -> "TensorRTEngineBuilder":
        """

        :param tp_degree
        :param pp_degree
        :param world_size:
        :param num_gpus_per_node:
        :return:
        """
        # if self._sharding_info:
        #     raise Exception(f"Cannot specify twice sharding config ({self._sharding_info})")

        LOGGER.debug(
            f"Setting sharding strategy to world_size={world_size}, num_gpus_per_node={num_gpus_per_node}"
        )
        self._sharding_info = ShardingInfo(
            tp_degree, pp_degree, world_size, num_gpus_per_node
        )

        return self

    def with_quantization_profile(
        self, mode: QuantMode, calibration: Optional[Calibration] = None
    ) -> "TensorRTEngineBuilder":
        """

        :param mode:
        :param calibration:
        :return:
        """
        if mode.has_fp8_qdq() or mode.has_fp8_kv_cache():
            from optimum.nvidia.utils.nvml import has_float8_support

            if not has_float8_support():
                raise UnsupportedHardwareFeature.float8()

        # TODO: validate the calibration is required or not
        self._qconfig = mode
        self._quantization_calibration = calibration

        return self

    def with_generation_profile(
        self,
        max_batch_size: int,
        max_prompt_length: Optional[int] = None,
        max_new_tokens: Optional[int] = None,
        max_output_length: Optional[int] = None,
    ) -> "TensorRTEngineBuilder":
        if (
            max_output_length is None
            and max_prompt_length is not None
            and max_new_tokens is not None
        ):
            # TODO: Understand why we can set to a larger value?
            # max_output_length = self._model_config.max_sequence_length
            max_output_length = max_prompt_length + max_new_tokens

        LOGGER.debug(
            f"Defining generation profile: "
            f"max_batch_size={max_batch_size}, "
            f"max_prompt_length={max_prompt_length}, "
            f"max_new_tokens={max_new_tokens}",
            f"max_output_length={max_output_length}",
        )
        self._optimization_profile = OptimizationProfile(
            max_batch_size, max_prompt_length, max_new_tokens, max_output_length
        )

        return self

    def with_sampling_strategy(self, num_beams: int) -> "TensorRTEngineBuilder":
        """

        :param num_beams:
        :return:
        """
        LOGGER.debug(f"Enabling sampling with strategy: num_beams={num_beams}")
        self._beam_width = num_beams
        return self

    def validate(self) -> bool:
        if self._qconfig is None:
            LOGGER.warning(
                "Quantization descriptor was None, assuming no quantization will be applied. "
                "If you want to change this behaviour, please use TRTEngineBuilder.with_quantization_schema()"
            )
            self._qconfig = (QuantMode(0), 0)

        # Optimization profile
        if self._optimization_profile is None:
            raise ValueError(
                "No optimization profile has been defined, please do set the profile you want this engine "
                "to be optimized for through TRTEngineBuilder.with_optimization_profile()."
            )

        # Ensure ranges are compatible
        optim_profile = self._optimization_profile
        model_config = self._model_config
        max_sequence_length = model_config["max_position_embeddings"]  # for now
        for prop, (min_value, max_value) in [
            ("max_batch_size", (1, None)),
            ("max_prompt_length", (1, max_sequence_length - 1)),
            ("max_new_tokens", (1, max_sequence_length - 1)),
            (
                "max_output_length",
                (
                    optim_profile.max_prompt_length + optim_profile.max_new_tokens,
                    max_sequence_length,
                ),
            ),
        ]:
            prop_value = getattr(optim_profile, prop)
            if prop_value < min_value:
                raise ValueError(
                    f"Invalid value ({prop_value}) for {prop}. Needs to be >= {min_value}"
                )

            if max_value is not None and prop_value > max_value:
                raise ValueError(
                    f"Invalid value ({prop_value}) for {prop}. Needs to be <= {max_value}"
                )

        if (
            optim_profile.max_prompt_length + optim_profile.max_new_tokens
            > max_sequence_length
        ):
            new_max_new_tokens = max_sequence_length - optim_profile.max_prompt_length
            LOGGER.warning(
                f"max_prompt_tokens ({optim_profile.max_prompt_length}) + max_new_tokens ({optim_profile.max_new_tokens})"
                f" is longer than model's maximum sequence length ({max_sequence_length}). "
                f"Truncating the max_new_tokens to {new_max_new_tokens}."
            )

        # Sampling info
        if self._beam_width < 1:
            LOGGER.warning(
                "Sampling strategy was not specified, defaulting to greedy search. "
                "If you want to define another sampling strategy, please use TRTEngineBuilder.with_sampling_strategy()."
            )
            self._beam_width = 1

        return True

    def quantize(self, output_path: Path):
        from optimum.nvidia.quantization.ammo import AmmoQuantizer

        LOGGER.debug(f"Model requires quantization (mode: {self._qconfig})")

        # Save quantization artifacts
        calibration_path = output_path.joinpath("calibration")

        # Handle any calibration required for static quantization
        if self._quantization_calibration:
            has_json = has_qweight = False
            if calibration_path.exists() and calibration_path.is_dir():
                calibration_data = os.listdir(calibration_path)
                if len(calibration_data) == 2:
                    has_json = any(f.endswith(".json") for f in calibration_data)
                    has_qweight = any(
                        f.endswith(".safetensors") for f in calibration_data
                    )

            if not calibration_path.exists() or not (has_json and has_qweight):
                LOGGER.info("Calibrating model...")

                # Retrieve device total memory
                fraction_device_map = {
                    device_id: get_device_memory(device_id) * 0.7
                    for device_id in range(get_device_count())
                }

                cpu_device_map = {"cpu": virtual_memory().available * 0.8}

                # Allocate required components for quantization
                hf_model = self.LOADING_CLASS.from_pretrained(
                    self._model_id_or_path,
                    device_map="balanced",
                    torch_dtype=DataType(self._dtype).to_torch(),
                    max_memory=fraction_device_map | cpu_device_map,
                ).to(memory_format=torch.channels_last)

                hf_model = maybe_offload_weights_to_cpu(hf_model)

                quantizer = AmmoQuantizer(
                    hf_model,
                    self._qconfig,
                    self._dtype,
                    self._sharding_info.tp_degree,
                    quantizer_overrides={
                        "*lm_head*": {"enable": False},
                    },
                )
                quantizer.calibrate(self._quantization_calibration)
                quantizer.save(calibration_path)
                # Release the memory
                del hf_model
                torch.cuda.empty_cache()
            else:
                LOGGER.info(
                    f"Reusing already precomputed calibration data at {calibration_path}"
                )

        self._build_info = BuildInfo(
            self._build_info.parallel,
            self._build_info.num_parallel_jobs,
            calibration_path,
        )

    def build(self, output_path: PathLike, optimization_level: int = None) -> PathLike:
        # Sharding info
        sharding = self._sharding_info or NO_SHARDING
        shards_info = [
            Shard(
                sharding.world_size,
                rank,
                sharding.num_gpus_per_node,
                sharding.tp_degree,
                sharding.pp_degree,
            )
            for rank in range(sharding.world_size)
        ]

        output_path = Path(output_path)
        if not output_path.exists():
            output_path.mkdir(parents=True)

        # Handle potential need for computing calibration data to quantize the model
        if self._qconfig:
            self.quantize(output_path)

        if self.validate():
            if self._build_info.parallel and self._build_info.num_parallel_jobs > 1:
                build_func = self._build_parallel
            else:
                build_func = self._build_serial

            # Let's build
            build_func(shards_info, output_path, optimization_level)
            return output_path

    def _build_serial(
        self,
        shards_info: List[Shard],
        output_path: Path,
        opt_level: Optional[int],
    ):
        LOGGER.debug("Building TRT engines sequentially")

        for shard in shards_info:
            self._build_engine_for_rank(
                shard, output_path, opt_level, is_parallel=False
            )

    def _build_parallel(
        self,
        shard_info: List[Shard],
        output_path: Path,
        opt_level: Optional[int],
    ):
        build_info = self._build_info
        num_jobs = (
            build_info.num_parallel_jobs
            if build_info.num_parallel_jobs > 1
            else sched_getaffinity(0)
        )

        # If there are more CPU cores than rank ... Let's reduce the number of jobs
        if num_jobs > len(shard_info):
            num_jobs = shard_info

        LOGGER.debug(f"Building TRT engines in parallel ({num_jobs} processes)")
        with Pool(num_jobs) as builders:
            for shard in shard_info:
                _ = builders.map(
                    self._build_engine_for_rank,
                    shard,
                    output_path,
                    is_parallel=True,
                    opt_level=opt_level,
                )

    def _build_engine_for_rank(
        self,
        shard: Shard,
        output_path: Path,
        opt_level: Optional[int],
        is_parallel: bool,
    ):
        LOGGER.debug(
            f"Building engine rank={shard.rank} (world_size={shard.world_size})"
        )

        config = self._model_config
        qconfig = self._qconfig

        ranked_engine_name = create_unique_engine_name(
            config["model_type"], self._dtype, shard.rank, shard.tp_size
        )

        builder = Builder()
        build_config = self.create_builder_config(
            tensorrt_llm_builder=builder,
            shard=shard,
            is_parallel=is_parallel,
            opt_level=opt_level,
        )

        # Let's build the network
        network = builder.create_network()
        network.trt_network.name = ranked_engine_name

        # Enable plugins
        network.plugin_config.set_gpt_attention_plugin(dtype=self._dtype)
        network.plugin_config.set_bert_attention_plugin(dtype=self._dtype)
        network.plugin_config.set_context_fmha(ContextFMHAType.enabled)
        network.plugin_config.enable_remove_input_padding()

        # GeMM plugin doesn't support float8
        if not build_config.fp8:
            network.plugin_config.set_gemm_plugin(dtype=self._dtype)

        network.plugin_config.enable_xqa = False
        network.plugin_config.paged_kv_cache = False

        if shard.world_size > 1:
            LOGGER.debug(f"Enabling NCCL plugin as world_size = ({shard.world_size})")
            network.plugin_config.set_nccl_plugin(dtype=self._dtype)

        # Set the weights
        with net_guard(network):
            adapter = _MODEL_TYPE_TO_TRT_IMPL[self._model_config["model_type"]]
            if build_config.fp8 and self._build_info.quantized_path is not None:
                model = adapter.from_checkpoint(self._build_info.quantized_path)
            else:
                model = adapter.from_hugging_face(
                    hf_model_dir=self._model_id_or_path,
                    dtype=self._dtype,
                    mapping=shard,
                    quant_mode=qconfig,
                )

            network.set_named_parameters(model.named_parameters())
            inputs = self.prepare_inputs(model)
            model(**inputs)

            if parse_flag_from_env("OPTIMUM_NVIDIA_ENABLE_DEBUG_OUTPUTS", False):
                LOGGER.info("Enabling dumping hidden tensor's output in debug mode")
                for k, v in model.named_network_outputs():
                    network._mark_output(k, v, DataType(self._dtype).to_trt())

            if parse_flag_from_env("OPTIMUM_NVIDIA_OUTPUT_ONNX_IR", False):
                from optimum.nvidia.utils import to_onnx

                to_onnx(network.trt_network, output_path.joinpath("model.onnx"))

        LOGGER.debug("Optimizing network ...")
        graph_rewriting.optimize(network)

        # Let's build the engine
        engine = builder.build_engine(network, build_config)

        # Store the build config for the master (rank = 0) to avoid writing up multiple times the same thing
        if shard.rank == 0:
            hf_config_path = output_path.joinpath(CONFIG_NAME)
            build_config_path = output_path.joinpath(OPTIMUM_NVIDIA_CONFIG_FILE)
            timings_path = output_path.joinpath(TENSORRT_TIMINGS_FILE)

            # Save the model's configuration (mainly to restore with from_pretrained without too much pain)
            with open(hf_config_path, "w", encoding="utf-8") as hf_config_f:
                config_ = config.config if hasattr(config, "config") else config
                json.dump(config_, hf_config_f)
                LOGGER.debug(f"Saved HF model config at {hf_config_path}")

            # Save the computed timings
            builder.save_timing_cache(build_config, str(timings_path))
            LOGGER.debug(f"Saved rank 0 timings at {timings_path}")

            # Save builder config holding all the engine specificities
            builder.save_config(build_config, str(build_config_path))
            LOGGER.debug(f"Saved engine config at {build_config_path}")

        if not engine:
            raise RuntimeError(
                "TRT Engine build failed... Please check the logs and open up an issue."
            )
        self._serialize_engine(engine, output_path.joinpath(ranked_engine_name))

    def _serialize_engine(self, engine, path: Path):
        LOGGER.info(f"Saving engine to {path}...")
        with open(path, "wb") as f:
            f.write(bytearray(engine))

    def prepare_inputs(self, model):
        """
        Prepares inputs to be run by the model. This is kept for backward compatibility in the base class for Llama, but this should be overridden for each architecture as prepare_inputs takes different arguments depending on the tensorrt_llm.Module subclass.
        """
        print(model.config.to_dict())
        inputs = model.prepare_inputs(
            max_batch_size=self._optimization_profile.max_batch_size,
            max_input_len=self._optimization_profile.max_prompt_length,
            max_seq_len=self._model_config["max_position_embeddings"],
            max_num_tokens=self._optimization_profile.max_new_tokens,
            max_beam_width=self._beam_width,
            use_cache=True,
        )

        return inputs

    def create_builder_config(
        self,
        tensorrt_llm_builder: Builder,
        shard: Shard,
        is_parallel: bool,
        opt_level: Optional[int],
    ):
        """
        Prepares the builder for the model. This is kept for backward compatibility in the base class for Llama, but this should be overridden for each architecture as `Builder.create_builder_config` takes different arguments depending on the architecture.
        """
        config = self._model_config
        qconfig = self._qconfig

        build_config = tensorrt_llm_builder.create_builder_config(
            name=config["model_type"],
            precision=self._dtype,
            fp8=qconfig.has_fp8_qdq(),
            hidden_size=config["hidden_size"],
            num_layers=config["num_hidden_layers"],
            max_batch_size=self._optimization_profile.max_batch_size,
            tensor_parallel=shard.tp_size,
            use_refit=False,
            quant_mode=self._qconfig,
            huggingface=dict(**config),
            hidden_act=config["hidden_act"],
            num_kv_heads=config.get(
                "num_key_value_heads", config["num_attention_heads"]
            ),
            num_heads=config["num_attention_heads"],
            max_position_embeddings=config["max_position_embeddings"],
            max_input_len=self._optimization_profile.max_prompt_length,
            max_output_len=self._optimization_profile.max_output_length,
            max_num_tokens=None,
            max_beam_width=self._beam_width,
            strongly_typed=qconfig.has_fp8_qdq(),
            pipeline_parallel=shard.pp_size,
            parallel_build=is_parallel,
            vocab_size=config["vocab_size"],
            opt_level=opt_level,
        )

        return build_config
