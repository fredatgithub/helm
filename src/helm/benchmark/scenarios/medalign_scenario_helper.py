# flake8: noqa
# type: ignore
# fmt: off

import transformers
import os
import pandas as pd
import tiktoken

from tqdm import tqdm
from typing import Any, Dict, Optional, Callable

from helm.common.general import check_file_exists


def get_instructions(path_to_instructions: str) -> Dict[int, Dict[str, Any]]:
    """
    Builds map from Instruction ID to instruction details

    The needed information for creating the map is accomplished by reading
    a CSV file from the user-specified path.

    The CSV file is expected to contain at least the following columns:
    - instruction_id: The ID of the instruction.
    - question: The text of the instruction.
    - person_id: The ID of the associated patient.
    - is_selected_ehr: A flag indicating whether the instruction is selected.

    See https://stanfordmedicine.box.com/s/0om9qav2sklb9vaitn0ibye65vgbfx0e

    Parameters:
        path_to_instructions (str): Path to CSV file containing instructions.

    Returns:
        Dict[int, Dict[str, Any]]: A dictionary mapping instruction IDs to a
            dictionary containing instruction text and associated patient ID.

    Raises:
        FileNotFoundError: If the specified file does not exist.
        ValueError: If the CSV file does not contain the expected columns.
    """
    if not os.path.exists(path_to_instructions):
        raise FileNotFoundError(
            f"The specified file {path_to_instructions} does not exist."
        )

    instructions_df = pd.read_csv(path_to_instructions, sep='\t')
    required_columns = {
        "instruction_id",
        "question",
        "person_id",
    }
    if not required_columns.issubset(instructions_df.columns):
        raise ValueError(
            f"The CSV file is missing one or more of the required columns: {required_columns}"
        )

    selected_instructions_df = instructions_df #.query("is_selected_ehr == 'yes'")
    instructions_map = {
        row["instruction_id"]: {
            "instruction": row["question"],
            "patient_id": row["person_id"],
        }
        for _, row in selected_instructions_df.iterrows()
    }
    return instructions_map


def extract_patient_id_from_fname(fname: str) -> Optional[int]:
    """
    Extracts and returns the patient ID from a given filename.

    The function expects filenames in the format 'EHR_<patient_id>.xml',
    where <patient_id> is a sequence of digits.

    Parameters:
        fname (str): The filename from which to extract the patient ID.

    Returns:
        Optional[int]: The extracted patient ID as an integer, or None if
                    the filename doesn't match the expected format.
    """
    name=fname.split('.')[0]
    return int(name)


def get_ehrs(path_to_ehrs: str) -> Dict[int, str]:
    """
    Builds a map from Instruction ID to EHR (Electronic Health Record) timeline.

    EHR timelines are in string format and EHR files are read in from the
    user-specified directory. Each file in the directory should be named
    'EHR_<patient_id>.xml', where <patient_id> is a sequence of digits.

    See https://stanfordmedicine.box.com/s/r28wfwwude9rpjtu0szhzegmku8qv2pe

    Parameters:
        path_to_ehrs (str): The path to the directory containing the EHR files.

    Returns:
        Dict[int, str]: A dictionary mapping patient IDs to EHR timelines.

    Raises:
        FileNotFoundError: If the specified directory does not exist.
    """
    if not os.path.isdir(path_to_ehrs):
        raise FileNotFoundError(
            f"The specified directory {path_to_ehrs} does not exist."
        )

    ehr_map = {}
    for fname in os.listdir(path_to_ehrs):
        pt_id = extract_patient_id_from_fname(fname)
        if pt_id is None:
            print(
                f"Warning: File '{fname}' does not match the expected format "
                "and will be skipped."
            )
            continue

        file_path = os.path.join(path_to_ehrs, fname)
        with open(file_path, encoding="utf-8", mode="r") as f:
            ehr = f.read()

        ehr_map[pt_id] = ehr
    return ehr_map


def get_tokenizer(tokenizer_name: str) -> Callable:
    """
    Returns a tokenizer based on the given tokenizer name.

    Parameters:
        tokenizer_name (str): The name of the tokenizer. Acceptable values are:
            - "tiktoken"
            - "chatgpt"
            - "gpt-3.5-turbo"
            - "gpt-4"
            - "gpt-4-turbo"
            - "gpt-4o"
            - "cl100k_base"
            - Any valid tokenizer name recognized by the transformers library.

    Returns:
        Callable: The tokenizer instance.
    """
    if tokenizer_name.lower() in [
        "tiktoken",
        "chatgpt",
        "gpt-3.5-turbo",
        "gpt-4",
        "gpt-4-turbo",
        "gpt-4o",
        "cl100k_base",
    ]:
        return tiktoken.get_encoding("cl100k_base")
    print(tokenizer_name)
    return transformers.AutoTokenizer.from_pretrained(tokenizer_name, legacy=False)


def pack_and_trim_prompts(
    instructions: Dict[int, Dict[str, str]],
    ehrs: Dict[int, str],
    prompt_string: str,
    context_length: int,
    generation_length: int,
    tokenizer: Any,
    verbose: bool = False,
    include_ehr: bool = True,
) -> Dict[int, str]:
    """
    Returns:
        A map from Instruction ID to prompt
    """
    prompts_map = {}
    for instruction_id in tqdm(instructions.keys()):
        instruction = instructions[instruction_id]["instruction"]
        patient_id = int(instructions[instruction_id]["patient_id"])
        relevant_ehr = ehrs[patient_id]

        num_tokens_instruction = len(tokenizer.encode(instruction))
        num_tokens_prompt_template = len(tokenizer.encode(prompt_string))
        if include_ehr:
            target_ehr_length = context_length - generation_length - num_tokens_prompt_template - num_tokens_instruction
        else:
            target_ehr_length = 0
        if target_ehr_length <= 0:
            prompt_with_truncated_ehr = prompt_string.format(question=instruction, ehr="")
        else:
            # Do a first pass with a fast tokenizer
            fast_tokenizer = tiktoken.get_encoding("cl100k_base")
            fast_encoded = fast_tokenizer.encode(relevant_ehr)
            if len(fast_encoded) <= target_ehr_length:
                fast_encoded_truncated = fast_encoded[-(2 * target_ehr_length) :]
                fast_truncated_ehr = fast_tokenizer.decode(fast_encoded_truncated)

                # Then do a second pass with the actual tokenizer
                encoded_ehr = tokenizer.encode(fast_truncated_ehr)
                truncated_encoded_ehr = encoded_ehr[-target_ehr_length:]
                truncated_ehr = tokenizer.decode(truncated_encoded_ehr)
                prompt_with_truncated_ehr = prompt_string.format(question=instruction, ehr=truncated_ehr)
            else:
                # If the fast encoding is still too long, just use the full EHR up to allowed length
                truncated_ehr = fast_tokenizer.decode(fast_encoded[-target_ehr_length:])
                prompt_with_truncated_ehr = prompt_string.format(question=instruction, ehr=truncated_ehr)

        prompts_map[instruction_id] = prompt_with_truncated_ehr

        if verbose:
            print(prompt_with_truncated_ehr)
            print("~" * 20)
    return prompts_map


def preprocess_prompts(
    target_context_length,
    generation_length,
    path_to_instructions,
    path_to_ehrs,
    include_ehr,
    tokenizer,
    codes_only=False,
    notes_only=False,
):
    print(
        f"\n\twith target context length = {target_context_length} "
        f"\n\twith target generation length = {generation_length} "
    )

    # FETCH INSTRUCTIONS
    print("Fetching instructions...")
    instructions = get_instructions(path_to_instructions)

    # FETCH RELEVANT EHRs #
    print("Fetching patient EHR timelines...")
    ehrs = get_ehrs(path_to_ehrs)

    # LOAD TOKENIZER #
    print("Loading tokenizer...")
    tokenizer = get_tokenizer(tokenizer)

    # CONSTRUCT & TRUNCATE PROMPTS #
    print("Constructing prompts using instructions and EHRs...")
    prompt_string = (
        "Instruction: Answer the following question based on the EHR:\n\n"
        "EHR: {ehr}\n\nQuestion: {question}\n\nAnswer:"
    )

    filled_prompts = pack_and_trim_prompts(
        instructions=instructions,
        ehrs=ehrs,
        prompt_string=prompt_string,
        context_length=target_context_length,
        generation_length=generation_length,
        tokenizer=tokenizer,
        verbose=False,
        include_ehr=include_ehr,
    )
    assert filled_prompts, f"No prompts were found for length: {target_context_length}. Try again with a larger length."
    # SAVE CONSTRUCTED PROMPTS TO DISK
    df_rows = []
    for instruction_id in tqdm(filled_prompts.keys()):
        row = {}
        row["instruction_id"] = instruction_id
        patient_id = instructions[instruction_id]["patient_id"]
        row["patient_id"] = patient_id
        row["instruction"] = instructions[instruction_id]["instruction"]
        row["ehr"] = "".join(ehrs[patient_id])
        row["prompt"] = filled_prompts[instruction_id]
        row["context_length"] = target_context_length
        row["generation_length"] = generation_length
        df_rows.append(row)

    prompts_df = pd.DataFrame(df_rows)
    instructionid_to_prompt_map = (
        prompts_df[["instruction_id", "prompt"]].set_index("instruction_id").to_dict().get("prompt")
    )
    instructionid_to_prompt_df = (
        pd.DataFrame.from_dict(instructionid_to_prompt_map, orient="index", columns=["prompt"])
        .reset_index()
        .rename(columns={"index": "instruction_id"})
    )

    print("...Prompt construction complete")
    return instructionid_to_prompt_df


def add_reference_responses(prompts_df, path_to_reference_responses) -> pd.DataFrame:
    """
    Processes a single file for evaluation.

    Parameters:
    file_path (str): Path to the file to be processed.
    args (argparse.Namespace): Command line arguments passed to the script.

    Returns:
    pd.DataFrame: DataFrame containing the processed data.
    """
    gold_df = pd.read_csv(path_to_reference_responses, sep='\t')
    gold_df = gold_df.query("annotator_num == 'Annotator_1'")
    gold_df = gold_df[["instruction_id", "clinician_response"]]
    merged_df = gold_df.merge(prompts_df, on="instruction_id", how="inner")
    return merged_df


def return_dataset_dataframe(max_length: int, data_path: str) -> pd.DataFrame:
    target_context_length = max_length
    generation_length = 256
    path_to_instructions = os.path.join(data_path, "clinician-reviewed-model-responses.tsv")
    check_file_exists(path_to_instructions, msg=f"[MedAlignScenario] Required instructions file not found: '{path_to_instructions}'")
    path_to_ehrs = os.path.join(data_path, "medalign_ehr_xml")
    path_to_reference_responses = os.path.join(data_path, "clinician-instruction-responses.tsv")
    check_file_exists(path_to_reference_responses, msg=f"[MedAlignScenario] Required clinician responses file not found: '{path_to_reference_responses}'")
    include_ehr = True
    tokenizer = "tiktoken"

    instructionid_to_prompt_df = preprocess_prompts(
        target_context_length=target_context_length,
        generation_length=generation_length,
        path_to_instructions=path_to_instructions,
        path_to_ehrs=path_to_ehrs,
        include_ehr=include_ehr,
        tokenizer=tokenizer,
    )
    medalign_dataframe = add_reference_responses(instructionid_to_prompt_df, path_to_reference_responses)
    return medalign_dataframe
