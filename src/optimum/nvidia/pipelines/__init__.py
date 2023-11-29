from os import PathLike
from typing import Dict, Optional, Type, Tuple, Union

from tensorrt_llm import Module
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast, AutoTokenizer, AutoConfig

from optimum.nvidia import TensorRTForCausalLM, AutoModelForCausalLM
from optimum.nvidia.pipelines.text_generation import TextGenerationPipeline
from optimum.nvidia.utils import get_user_agent

from .base import Pipeline

SUPPORTED_MODEL_WITH_TASKS: Dict[str, Dict[str, Tuple[Type[Pipeline], Type]]] = {
    "llama": {
        "text-generation": (TextGenerationPipeline, AutoModelForCausalLM)
    }
}

def get_target_class_for_model_and_task(task: str, architecture: str) -> Optional[Type]:
    task_ = SUPPORTED_MODEL_WITH_TASKS.get(task, None)
    if not task:
        raise NotImplementedError(f"Task {task} is not supported yet.")

    target = task_.get(architecture, None)

    if not target:
        raise NotImplementedError(
            f"Architecture {architecture} is not supported for task {task}. "
            f"Only the following architectures are: {list(task_.keys())}"
        )

    return target


def pipeline(
    task: str,
    model_or_path: Union[str, PathLike, Module],
    tokenizer: Optional[Union[str, PreTrainedTokenizer, PreTrainedTokenizerFast]] = None,
    **kwargs
):
    """
    Utility factory method to build a [`Pipeline`].

    Pipelines are made of:

        - A [tokenizer](tokenizer) in charge of mapping raw textual input to token.
        - A [model](model) to make predictions from the inputs.
        - Some (optional) post processing for enhancing model's output.

    Args:
        task (`str`):
            The task defining which pipeline will be returned. Currently accepted tasks are:
                - `"text-generation"`: will return a [`TextGenerationPipeline`]:.
        model_or_path (`str` or [`PreTrainedModel`] or [`TFPreTrainedModel`], *optional*):
            The model that will be used by the pipeline to make predictions. This can be a model identifier or an
            actual instance of a pretrained model inheriting from [`PreTrainedModel`] (for PyTorch) or
            [`TFPreTrainedModel`] (for TensorFlow).

            If not provided, the default for the `task` will be loaded.
        tokenizer (`str` or [`PreTrainedTokenizer`], *optional*):
            The tokenizer that will be used by the pipeline to encode data for the model. This can be a model
            identifier or an actual pretrained tokenizer inheriting from [`PreTrainedTokenizer`].

            If not provided, the default tokenizer for the given `model` will be loaded (if it is a string). If `model`
            is not specified or not a string, then the default tokenizer for `config` is loaded (if it is a string).
            However, if `config` is also not given or not a string, then the default tokenizer for the given `task`
            will be loaded.

    """

    config = AutoConfig.from_pretrained(model_or_path, user_agent=get_user_agent())
    model_type = config.model_type

    if model_type not in SUPPORTED_MODEL_WITH_TASKS:
        raise NotImplementedError(f"Model type {model_type} is not currently supported")

    if task not in SUPPORTED_MODEL_WITH_TASKS[model_type]:
        raise ValueError(f"Task {task} is not supported yet for {model_type}.")

    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(model_or_path, use_fast=True)

    # Allocate
    pipeline_factory, model_factory = SUPPORTED_MODEL_WITH_TASKS[model_type][task]

    if not isinstance(model_or_path, TensorRTForCausalLM):
        model = model_factory.from_pretrained(model_or_path, **kwargs)
    else:
        model = model_or_path

    return pipeline_factory(model, tokenizer)
