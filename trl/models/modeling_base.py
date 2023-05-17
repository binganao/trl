# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import os
from copy import deepcopy

import torch
import torch.nn as nn
from accelerate import Accelerator
from huggingface_hub import hf_hub_download
from transformers import PreTrainedModel

from ..import_utils import is_peft_available


if is_peft_available():
    from peft import (
        LoraConfig,
        PeftConfig,
        PeftModel,
        PeftModelForCausalLM,
        PeftModelForSeq2SeqLM,
        get_peft_model,
        prepare_model_for_int8_training,
    )
    from peft.peft_model import set_peft_model_state_dict

LAYER_PATTERNS = [
    "transformer.h.{layer}",
    "model.decoder.layers.{layer}",
    "gpt_neox.layers.{layer}",
    "model.layers.{layer}",
]


class PreTrainedModelWrapper(nn.Module):
    r"""
    A wrapper class around a (`transformers.PreTrainedModel`) to be compatible with the
    (`~transformers.PreTrained`) class in order to keep some attributes and methods of the
    (`~transformers.PreTrainedModel`) class.

    Attributes:
        pretrained_model: (`transformers.PreTrainedModel`)
            The model to be wrapped.
        parent_class: (`transformers.PreTrainedModel`)
            The parent class of the model to be wrapped.
        supported_args: (`list`)
            The list of arguments that are supported by the wrapper class.
    """
    transformers_parent_class = None
    supported_args = None
    supported_modules = ("v_head",)
    supported_rm_modules = ("score",)
    supported_pretrained_model_architectures = (
        (PreTrainedModel)
        if not is_peft_available()
        else (PreTrainedModel, PeftModelForCausalLM, PeftModelForSeq2SeqLM)
    )

    def __init__(self, pretrained_model=None, **kwargs):
        super().__init__()
        self.pretrained_model = pretrained_model

        self.config = pretrained_model.config
        self.prepare_inputs_for_generation = pretrained_model.prepare_inputs_for_generation
        self.is_loaded_in_8bit = getattr(pretrained_model, "is_loaded_in_8bit", False)
        self.is_sequential_parallel = False

        if hasattr(pretrained_model, "gradient_checkpointing_disable"):
            self.gradient_checkpointing_disable = pretrained_model.gradient_checkpointing_disable

        if hasattr(pretrained_model, "gradient_checkpointing_enable"):
            self.gradient_checkpointing_enable = pretrained_model.gradient_checkpointing_enable

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path, *model_args, **kwargs):
        r"""
        Instantiates a new model from a pretrained model from `transformers`. The
        pretrained model is loaded using the `from_pretrained` method of the
        `transformers.PreTrainedModel` class. The arguments that are specific to the
        `transformers.PreTrainedModel` class are passed along this method and filtered
        out from the `kwargs` argument.


        Args:
            pretrained_model_name_or_path (`str` or `transformers.PreTrainedModel`):
                The path to the pretrained model or its name.
            *model_args (`list`, *optional*)):
                Additional positional arguments passed along to the underlying model's
                `from_pretrained` method.
            **kwargs (`dict`, *optional*):
                Additional keyword arguments passed along to the underlying model's
                `from_pretrained` method. We also pre-process the kwargs to extract
                the arguments that are specific to the `transformers.PreTrainedModel`
                class and the arguments that are specific to trl models. The kwargs
                also support `prepare_model_for_int8_training` arguments from
                `peft` library.
        """
        if kwargs is not None:
            peft_config = kwargs.pop("peft_config", None)
            reward_modeling_adapter_id = kwargs.pop("reward_modeling_adapter_id", None)
            is_trainable = kwargs.pop("is_trainable", False)
            trl_model_args, pretrained_kwargs, peft_int8_kwargs = cls._split_kwargs(kwargs)
        else:
            peft_config = None
            is_trainable = False
            trl_model_args = {}
            pretrained_kwargs = {}
            peft_int8_kwargs = {}

        if reward_modeling_adapter_id is not None and not isinstance(reward_modeling_adapter_id, str):
            raise ValueError(
                "The `reward_modeling_adapter_id` argument should be a string representing the name of local path or the Hub id to the Reward Modeling adapter."
            )

        is_peft_model = False
        current_device = cls._get_current_device()
        if isinstance(pretrained_model_name_or_path, str):
            is_loaded_in_8bit = pretrained_kwargs["load_in_8bit"] if "load_in_8bit" in pretrained_kwargs else False
        else:
            is_loaded_in_8bit = getattr(pretrained_model_name_or_path, "is_loaded_in_8bit", False)

        if is_loaded_in_8bit and "device_map" not in pretrained_kwargs:
            # warn users
            logging.warning(
                "The `device_map` argument is not provided. We will override the device_map argument."
                " to set the entire"
                " model on the current device. If you want to set the model on multiple devices, please provide"
                " a custom `device_map` argument."
            )
            pretrained_kwargs["device_map"] = {"": current_device}

        if is_peft_available() and peft_config is not None and not isinstance(peft_config, PeftConfig):
            raise ValueError("The `peft_config` argument should be an instance of `peft.PeftConfig` class.")

        # First, load the pre-trained model using the parent-class
        # either `AutoModelForCausalLM` or `AutoModelForSeq2SeqLM`
        if isinstance(pretrained_model_name_or_path, str):
            if is_peft_available():
                try:
                    peft_filename = hf_hub_download(pretrained_model_name_or_path, "adapter_config.json")
                except:  # noqa
                    peft_filename = None
            else:
                peft_filename = None

            # Dealing with `peft` case:
            # 1- check if `adapter_config` has been saved in the hub or locally
            # 2- if yes, load the `peft` config
            # 3- use the config to load the `transformers` model and then load the `peft` model
            if (
                os.path.exists(pretrained_model_name_or_path)
                and ("adapter_config.json" in os.listdir(pretrained_model_name_or_path) or peft_filename is not None)
                and is_peft_available()
            ):
                if peft_filename is not None:
                    peft_config = PeftConfig.from_pretrained(peft_filename)
                else:
                    peft_config = PeftConfig.from_pretrained(pretrained_model_name_or_path)

                pretrained_model = cls.transformers_parent_class.from_pretrained(
                    peft_config.base_model_name_or_path, *model_args, **pretrained_kwargs
                )

                pretrained_model = PeftModel.from_pretrained(
                    pretrained_model, pretrained_model_name_or_path, is_trainable=is_trainable
                )
            else:
                pretrained_model = cls.transformers_parent_class.from_pretrained(
                    pretrained_model_name_or_path, *model_args, **pretrained_kwargs
                )

                if peft_config is not None:
                    if is_loaded_in_8bit:
                        pretrained_model = prepare_model_for_int8_training(
                            pretrained_model,
                            **peft_int8_kwargs,
                        )
                    pretrained_model = get_peft_model(pretrained_model, peft_config)

        elif isinstance(pretrained_model_name_or_path, cls.supported_pretrained_model_architectures):
            pretrained_model = pretrained_model_name_or_path

            if peft_config is not None and isinstance(pretrained_model, PreTrainedModel):
                if is_loaded_in_8bit:
                    pretrained_model = prepare_model_for_int8_training(
                        pretrained_model,
                        **peft_int8_kwargs,
                    )
                pretrained_model = get_peft_model(pretrained_model, peft_config)
        else:
            raise ValueError(
                "pretrained_model_name_or_path should be a string or a PreTrainedModel, "
                f"but is {type(pretrained_model_name_or_path)}"
            )

        if is_peft_available():
            if isinstance(pretrained_model, PeftModel):
                is_peft_model = True
        # Then, create the full model by instantiating the wrapper class
        model = cls(pretrained_model, **trl_model_args)

        # if resume_training, load the state_dict again - this is ok since the
        # state_dict is removed from the model after loading it.
        is_resuming_training = True
        if isinstance(pretrained_model_name_or_path, str):
            filename = os.path.join(pretrained_model_name_or_path, "pytorch_model.bin")
            sharded_index_filename = os.path.join(pretrained_model_name_or_path, "pytorch_model.bin.index.json")
            is_shared = False

            if not os.path.exists(filename):
                try:
                    filename = hf_hub_download(pretrained_model_name_or_path, "pytorch_model.bin")
                # sharded
                except:  # noqa
                    if os.path.exists(sharded_index_filename):
                        index_file_name = sharded_index_filename
                    else:
                        try:
                            index_file_name = hf_hub_download(
                                pretrained_model_name_or_path, "pytorch_model.bin.index.json"
                            )
                        except ValueError:  # not continue training, do not have v_head weight
                            is_resuming_training = False
                            logging.warning(
                                f"A {type(pretrained_model)} model is loaded from '{pretrained_model_name_or_path}', "
                                f"and no v_head weight is found. This IS expected if you are not resuming PPO training."
                            )
                    # load json
                    if is_resuming_training:
                        with open(index_file_name, "r") as f:
                            index = json.load(f)
                        # check filename with `v_head` or any known extra module:
                        files_to_download = set()
                        for k, v in index["weight_map"].items():
                            if any([module in k for module in cls.supported_modules]):
                                files_to_download.add(v)
                        is_shared = True
            if is_resuming_training:
                if is_shared:
                    # download each file and add it to the state_dict
                    state_dict = {}
                    for shard_file in files_to_download:
                        filename = hf_hub_download(pretrained_model_name_or_path, shard_file)
                        state_dict.update(torch.load(filename, map_location="cpu"))
                else:
                    state_dict = torch.load(filename, map_location="cpu")

        else:
            state_dict = pretrained_model_name_or_path.state_dict()

        model.is_peft_model = is_peft_model
        model.current_device = current_device

        if is_resuming_training:
            model.post_init(state_dict=state_dict)

        if not is_peft_model and reward_modeling_adapter_id is not None:
            raise ValueError("reward_modeling_adapter_id can only be used with a PeftModel. ")
        elif is_peft_model and reward_modeling_adapter_id is not None:
            model.add_and_load_reward_modeling_adapter(reward_modeling_adapter_id)
            model.supports_rm_adapter = True
        else:
            model.supports_rm_adapter = False

        return model

    @classmethod
    def _get_current_device(cls):
        r"""
        Get the current device using the `Accelerate` object - We just return the
        process index of the `Accelerate` object to handle corner cases when running scripts
        in distributed setups.

        Returns:
            current_device (`int`):
                The current device index.
        """
        dummy_accelerator = Accelerator()
        current_device = dummy_accelerator.process_index
        return current_device

    @classmethod
    def _split_kwargs(cls, kwargs):
        """
        Separate the kwargs from the arguments that we support inside
        `supported_args` and the ones that we don't.
        """
        check_peft_kwargs = False

        if is_peft_available():
            from peft import prepare_model_for_int8_training

            check_peft_kwargs = True

        supported_kwargs = {}
        unsupported_kwargs = {}
        peft_kwargs = {}

        for key, value in kwargs.items():
            if key in cls.supported_args:
                supported_kwargs[key] = value
            else:
                unsupported_kwargs[key] = value

            if check_peft_kwargs:
                if key in prepare_model_for_int8_training.__code__.co_varnames:
                    peft_kwargs[key] = value
                    if key in unsupported_kwargs:
                        unsupported_kwargs.pop(key)

        return supported_kwargs, unsupported_kwargs, peft_kwargs

    def push_to_hub(self, *args, **kwargs):
        r"""
        Push the pretrained model to the hub. This method is a wrapper around
        `transformers.PreTrainedModel.push_to_hub`. Please refer to the documentation
        of `transformers.PreTrainedModel.push_to_hub` for more information.

        Args:
            *args (`list`, *optional*):
                Positional arguments passed along to the underlying model's
                `push_to_hub` method.
            **kwargs (`dict`, *optional*):
                Keyword arguments passed along to the underlying model's
                `push_to_hub` method.
        """
        raise NotImplementedError

    def save_pretrained(self, *args, **kwargs):
        r"""
        Save the pretrained model to a directory. This method is a wrapper around
        `transformers.PreTrainedModel.save_pretrained`. Please refer to the documentation
        of `transformers.PreTrainedModel.save_pretrained` for more information.

        Args:
            *args (`list`, *optional*):
                Positional arguments passed along to the underlying model's
                `save_pretrained` method.
            **kwargs (`dict`, *optional*):
                Keyword arguments passed along to the underlying model's
                `save_pretrained` method.
        """
        state_dict = kwargs.pop("state_dict", None)
        if state_dict is None:
            state_dict = self.state_dict()
            kwargs["state_dict"] = state_dict

        # if it is a peft model only save the `v_head` state_dict and
        # pop the `state_dict` from the kwargs to avoid slient bugs with `peft`
        if self.is_peft_model:
            save_path = args[0]
            save_path = os.path.join(save_path, "pytorch_model.bin")
            torch.save(state_dict, save_path)
            _ = kwargs.pop("state_dict", None)

        return self.pretrained_model.save_pretrained(*args, **kwargs)

    def state_dict(self, *args, **kwargs):
        r"""
        Return the state_dict of the pretrained model.
        """
        raise NotImplementedError

    def post_init(self, *args, **kwargs):
        r"""
        Post initialization method. This method is called after the model is
        instantiated and loaded from a checkpoint. It can be used to perform
        additional operations such as loading the state_dict.
        """
        raise NotImplementedError

    def add_and_load_reward_modeling_adapter(self, adapter_model_id, adapter_name="reward_model_adapter"):
        r"""
        Add and load a reward modeling adapter.
        """
        filename = os.path.join(adapter_model_id, "adapter_model.bin")
        if not os.path.exists(filename):
            try:
                local_filename = hf_hub_download(adapter_model_id, "adapter_model.bin")
            except: # noqa
                raise ValueError(
                    "Could not find adapter model in the Hub, make sure you have the correct adapter model id."
                )
        else:
            local_filename = filename

        adapter_state_dict = torch.load(local_filename, map_location="cpu")
        rm_adapter_peft_config = LoraConfig.from_pretrained(adapter_model_id)

        for score_name_candidate in self.supported_rm_modules:
            if any([score_name_candidate in name for name in adapter_state_dict.keys()]):
                score_name = score_name_candidate
                break

        score_dict = {}
        copy_adapter_state_dict = adapter_state_dict.copy()

        for name, _ in copy_adapter_state_dict.items():
            if score_name in name:
                key_name = ".".join(name.split(".")[-1:])
                score_dict[key_name] = adapter_state_dict.pop(name).to(self._get_current_device())

        self.pretrained_model.add_adapter(adapter_name, rm_adapter_peft_config)
        self.rm_adapter_name = adapter_name

        num_labels, hidden_dim = score_dict["weight"].shape
        has_bias = any(["bias" in name for name in adapter_state_dict.keys()])

        self.score = nn.Linear(hidden_dim, num_labels, bias=has_bias).to(self._get_current_device())
        self.score.load_state_dict(score_dict)

        # load the adapter to the model
        set_peft_model_state_dict(self.pretrained_model, adapter_state_dict, adapter_name=adapter_name)

    def compute_reward_score(self, input_ids, attention_mask, ppo_adapter_name="default", **kwargs):
        r"""
        Computes the reward score for a given input. The method has first to enable the adapter
        and then compute the reward score. After that the model disables the reward modeling
        adapter and enables the default ppo adapter again.
        """
        if not self.supports_rm_adapter:
            raise ValueError("This model does not support reward modeling adapter.")

        # enable rm adapter
        self.pretrained_model.set_adapter(self.rm_adapter_name)

        base_model_output = self.pretrained_model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_hidden_states=True,
            return_dict=True,
            **kwargs,
        )

        last_hidden_states = base_model_output.hidden_states[-1]
        scores = self.score(last_hidden_states)

        self.pretrained_model.set_adapter(ppo_adapter_name)

        return scores


def create_reference_model(
    model: PreTrainedModelWrapper, num_shared_layers: int = None, pattern: str = None
) -> PreTrainedModelWrapper:
    """
    Creates a static reference copy of a model. Note that model will be in `.eval()` mode.

    Args:
        model (`PreTrainedModelWrapper`): The model to be copied.
        num_shared_layers (`int`, *optional*): The number of initial layers that are shared between both models and kept frozen.
        pattern (`str`, *optional*): The shared layers are selected with a string pattern
            (e.g. "transformer.h.{layer}" for GPT2) and if a custom pattern is necessary it can be passed here.

    Returns
        `PreTrainedModelWrapper`
    """

    parameter_names = [n for n, _ in model.named_parameters()]
    ref_model = deepcopy(model)

    # if no layers are shared, return copy of model
    if num_shared_layers is None:
        for param_name in parameter_names:
            param = ref_model.get_parameter(param_name)
            param.requires_grad = False
        return ref_model.eval()

    # identify layer name pattern
    if pattern is not None:
        pattern = pattern.format(layer=num_shared_layers)
    else:
        for pattern_candidate in LAYER_PATTERNS:
            pattern_candidate = pattern_candidate.format(layer=num_shared_layers)
            if any([pattern_candidate in name for name in parameter_names]):
                pattern = pattern_candidate
                break

    if pattern is None:
        raise ValueError("Layer pattern could not be matched.")

    # divide parameters in shared and unshared parameter lists
    shared_param_list = []
    unshared_param_list = []

    shared_parameter = True
    for name, param in model.named_parameters():
        if pattern in name:
            shared_parameter = False
        if shared_parameter:
            shared_param_list.append(name)
        else:
            unshared_param_list.append(name)

    # create reference of the original parameter if they are shared
    for param_name in shared_param_list:
        param = model.get_parameter(param_name)
        param.requires_grad = False

        ref_param = ref_model.get_parameter(param_name)  # noqa
        ref_param = param  # noqa

    # for all other parameters just make sure they don't use gradients
    for param_name in unshared_param_list:
        param = ref_model.get_parameter(param_name)
        param.requires_grad = False

    if pattern is not None and len(unshared_param_list) == 0:
        logging.warning("Pattern passed or found, but no layers matched in the model. Check for a typo.")

    return ref_model.eval()
