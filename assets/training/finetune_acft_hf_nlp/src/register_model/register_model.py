# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run Model Registration module."""
import time
import argparse
from argparse import Namespace
from typing import Dict, Optional

import json
import re

from pathlib import Path
from azureml.core.model import Model

from azureml.core import Workspace
from azureml.core.run import Run, _OfflineRun

from azureml.acft.common_components import get_logger_app


logger = get_logger_app("azureml.acft.contrib.hf.scripts.components.scripts.register_model.register_model")


SUPPORTED_MODEL_ASSET_TYPES = [Model.Framework.CUSTOM, "PRESETS"]
# omitting underscores which is supported in model name for consistency
VALID_MODEL_NAME_PATTERN = r"^[a-zA-Z0-9-]+$"
NEGATIVE_MODEL_NAME_PATTERN = r"[^a-zA-Z0-9-]"
REGISTRATION_DETAILS_JSON_FILE = "model_registration_details.json"
DEFAULT_MODEL_NAME = "default_model_name"


def str2bool(arg):
    """Convert string to bool."""
    arg = arg.lower()
    if arg in ["true", '1']:
        return True
    elif arg in ["false", '0']:
        return False
    else:
        raise ValueError(f"Invalid argument {arg} to while converting string to boolean")


def parse_args():
    """Return arguments."""
    parser = argparse.ArgumentParser()

    # add arguments
    parser.add_argument("--model_path", type=str, help="Directory containing model files")
    parser.add_argument(
        "--convert_to_safetensors",
        type=str2bool,
        default="false",
        choices=[True, False],
        help="convert pytorch model to safetensors format"
    )
    parser.add_argument(
        "--copy_model_to_output",
        type=str2bool,
        default="false",
        choices=[True, False],
        help="If true, copies the model to output_dir"
    )
    parser.add_argument(
        "--model_type",
        type=str,
        default=Model.Framework.CUSTOM,
        choices=SUPPORTED_MODEL_ASSET_TYPES,
        help="Type of model you want to register",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        help="Name to use for the registered model. If it already exists, the version will be auto incremented.",
    )
    parser.add_argument(
        "--finetune_args_path",
        type=str,
        help="JSON file that contains the finetune information",
        default=None,
    )
    parser.add_argument(
        "--model_version",
        type=str,
        help="Model version in workspace/registry. If model with same version exists,version will be auto incremented",
        default=None,
    )
    parser.add_argument(
        "--registration_details_folder",
        type=Path,
        help="A folder which contains a JSON file into which model registration details will be written",
    )
    args = parser.parse_args()
    logger.info(f"Args received {args}")
    return args


def get_workspace_details() -> Workspace:
    """Fetch the workspace details from run context."""
    run = Run.get_context()
    if isinstance(run, _OfflineRun):
        return Workspace.from_config()
    return run.experiment.workspace


def is_model_available(ml_client, model_name, model_version):
    """Return true if model is available else false."""
    is_available = True
    try:
        ml_client.models.get(name=model_name, version=model_version)
    except Exception as e:
        logger.warning(f"Model with name - {model_name} and version - {model_version} is not available. Error: {e}")
        is_available = False
    return is_available


def get_model_name(finetune_args_path: str) -> Optional[str]:
    """Construct the model name from the base model."""
    import uuid
    with open(finetune_args_path, 'r', encoding="utf-8") as rptr:
        finetune_args_dict = json.load(rptr)

    try:
        base_model_name = finetune_args_dict.get("model_asset_id").split("/")[-3]
    except Exception:
        base_model_name = DEFAULT_MODEL_NAME
    logger.info(f"Base model name: {base_model_name}")

    new_model_name = base_model_name + "-ft-" + str(uuid.uuid4())
    logger.info(f"Updated model name: {new_model_name}")

    return new_model_name


def convert_lora_weights_to_safetensors(model_path: str):
    """Read the bin files and convert them to safe tensors."""
    import os
    import torch
    from azureml.acft.contrib.hf.nlp.utils.io_utils import find_files_with_inc_excl_pattern
    from safetensors.torch import save_file

    bin_files = find_files_with_inc_excl_pattern(model_path, include_pat=".bin$")
    logger.info(f"Following bin files are identified: {bin_files}")
    for bin_file in bin_files:
        bin_file_sd = torch.load(bin_file, map_location=torch.device("cpu"))
        safe_tensor_file = bin_file.replace(".bin", ".safetensors")
        save_file(bin_file_sd, safe_tensor_file)
        logger.info(f"Created {safe_tensor_file}")
        os.remove(bin_file)
        logger.info(f"Deleted {bin_file}")


def copy_model_to_output(model_path: str, output_dir: str):
    """Copy the model from model path to output dir."""
    import shutil
    logger.info("Started copying the model weights to output directory")
    shutil.copytree(model_path, output_dir, dirs_exist_ok=True)
    logger.info("Completed copying the weights")


def get_properties(finetune_args_path: str) -> Dict[str, str]:
    """Fetch the appropriate properties regarding the base model."""
    properties = {}
    with open(finetune_args_path, 'r', encoding="utf-8") as rptr:
        finetune_args_dict = json.load(rptr)

    # read from finetune config
    property_key_to_finetune_args_key_map = {
        "baseModelId": "model_asset_id",
    }
    for property_key, finetune_args_key in property_key_to_finetune_args_key_map.items():
        properties[property_key] = finetune_args_dict.get(finetune_args_key, None)
        if "baseModelId" == property_key:
            properties[property_key] = "/".join(properties[property_key].split('/')[:-2])

    # fixed properties
    additional_properties = {
        "baseModelWeightsVersion": 1.0,
    }
    properties.update(additional_properties)
    logger.info(f"Adding the following properties to the registered model: {properties}")

    return properties


def register_model(args: Namespace):
    """Run main function for sdkv1."""
    model_name = args.model_name
    model_type = args.model_type
    model_path = args.model_path
    registration_details_folder = args.registration_details_folder
    tags, properties, model_description = {}, {}, ""

    # set properties
    properties = get_properties(args.finetune_args_path)

    # create workspace details
    ws = get_workspace_details()

    if not re.match(VALID_MODEL_NAME_PATTERN, model_name):
        # update model name to one supported for registration
        logger.info(f"Updating model name to match pattern `{VALID_MODEL_NAME_PATTERN}`")
        model_name = re.sub(NEGATIVE_MODEL_NAME_PATTERN, "-", model_name)
        logger.info(f"Updated model_name = {model_name}")

    st = time.time()
    model = Model.register(
        workspace=ws,
        model_path=model_path,
        model_name=model_name,
        model_framework=model_type,
        description=model_description,
        tags=tags,
        properties=properties
    )
    time_to_register = time.time() - st
    logger.info(f"Time to register: {time_to_register} seconds")

    # register the model in workspace or registry
    logger.info(f"Registering model {model.name} with version {model.version}.")
    logger.info(f"Model registered. AssetID : {model.id}")
    # Registered model information
    model_info = {
        "id": model.id,
        "name": model.name,
        "version": model.version,
        "type": model.model_framework,
        "properties": model.properties,
        "tags": model.tags,
        "description": model.description,
    }
    json_object = json.dumps(model_info, indent=4)

    registration_file = registration_details_folder / REGISTRATION_DETAILS_JSON_FILE

    with open(registration_file, "w+") as outfile:
        outfile.write(json_object)
    logger.info("Saved model registration details in output json file.")


# run script
if __name__ == "__main__":
    args = parse_args()

    # convert to safe tensors
    if args.convert_to_safetensors:
        convert_lora_weights_to_safetensors(args.model_path)

    # update model name
    if args.model_name is None:
        args.model_name = get_model_name(args.finetune_args_path)

    # register model
    register_model(args)

    # copy to output dir
    if args.copy_model_to_output:
        copy_model_to_output(args.model_path, args.registration_details_folder)
