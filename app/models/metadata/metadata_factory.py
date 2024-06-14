import os
import re
import traceback
from functools import cache
from pathlib import Path
from typing import Any, Sequence

import msgspec
from loguru import logger

from app.models.metadata.metadata_structure import (
    BaseRules,
    CaseInsensitiveSet,
    CaseInsensitiveStr,
    DependencyMod,
    ExternalRulesSchema,
    ListedMod,
    LudeonMod,
    RuledMod,
    Rules,
)
from app.utils.constants import RIMWORLD_DLC_METADATA
from app.utils.xml import xml_path_to_json


class MalformedDataException(Exception):
    """
    Exception raised when the data given is detected to be malformed.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = f"Malformed data: {message}"


def value_extractor(
    input: dict[str, str] | dict[str, list[str]] | Sequence[str] | str,
) -> str | list[Any] | dict[str, str] | dict[str, list[str]]:
    """
    Extract the value from a mod_data entry.
    Stops at the outermost list or string.
    Stops if more than one key other than #text and @IgnoreIfNoMatchingField is found.

    :param input: The dictionary or string or list of strings to extract the value from.
    :return: The extracted value or the input string.
    :raises:

    """

    if isinstance(input, str):
        return input
    elif isinstance(input, Sequence):
        # Convert sequence to list
        return list(input)
    elif isinstance(input, dict):
        # If only one key, recurse into the value
        if len(input) == 1:
            return value_extractor(next(iter(input.values())))
        elif input.keys() == {"@IgnoreIfNoMatchingField", "#text"}:
            return input["#text"]
        else:
            return input


def match_version(
    input: dict[str, Any], target_version: str, stop_at_first: bool = False
) -> tuple[bool, None | list[Any]]:
    """Attempts to match an input key with the target version using regex.

    If the key is not found, the function returns None.
    If the matching key(s) is found, the function returns the value of the key(s) in a list.

    :param input: The dictionary to search for the key.
    :param target_version: The version to match. Should be of the format 'major.minor'.
    :param stop_at_first: If True, the function will return the first match found only."""
    major, minor = target_version.split(".")[:2]
    version_regex = rf"v{major}\.{minor}"

    results = []
    for key, value in input.items():
        if re.match(version_regex, key):
            if stop_at_first:
                return True, [value]
            results.append(value)

    if not results:
        return False, None

    return True, results


def create_listed_mod(
    mod_data: dict[str, Any], target_version: str
) -> tuple[bool, ListedMod]:
    """Factory method for creating a ListedMod object.

    :param mod_data: The dictionary containing the mod data.
    :param target_version: The version of RimWorld to target.
    :return: A tuple containing a boolean indicating if the mod is valid and the mod object."""
    mod = _parse_required(mod_data, RuledMod())

    if not isinstance(mod, RuledMod):
        ruled_mod = RuledMod()
        ruled_mod.__dict__ = mod.__dict__
        mod = ruled_mod

    mod = _parse_optional(mod_data, mod, target_version)

    return mod.valid, mod


def _set_mod_invalid(mod: ListedMod, message: str) -> ListedMod:
    """
    Set the mod to be invalid and log a warning message.

    :param mod: ListedMod to be set as valid False.
    :param message: The message to be logged as a warning
    :return: The ListedMod now set as invalid.
    """
    mod.valid = False
    logger.warning(message)
    return mod


def _parse_required(mod_data: dict[str, Any], mod: ListedMod) -> ListedMod:
    """
    Parse the required fields from the mod_data and set them on the mod object.

    :param mod_data: Dictionary with string keys to be used as the data source.
    :param mod: ListedMod the Listed mod that is the target of data being filled.
    :return: The filled out ListedMod
    """
    package_id = value_extractor(mod_data.get("packageId", False))
    if isinstance(package_id, str):
        mod.package_id = CaseInsensitiveStr(package_id)
    else:
        _set_mod_invalid(
            mod,
            f"packageId was not a string: {package_id}. This mod will be considered invalid by RimWorld.",
        )

    # Check if the package id is a known DLC package id
    if mod.package_id in get_dlc_packageid_appid_map():
        logger.info(f"Detected known Ludeon package id: {mod.package_id}.")
        mod = LudeonMod(
            **vars(mod), steam_app_id=int(get_dlc_packageid_appid_map()[mod.package_id])
        )

        mod.name = RIMWORLD_DLC_METADATA[str(mod.steam_app_id)]["name"]
        mod.description = RIMWORLD_DLC_METADATA[str(mod.steam_app_id)]["description"]
    elif "ludeon." in mod.package_id:
        logger.warning(
            f"Detected mod that is possibly a Ludeon mod with package id: {mod.package_id}. Could not be matched with known DLC package ids. If this is a DLC, please report it to the RimSort developers."
        )

        # Check if steamAppId is reported
        steam_app_id = value_extractor(mod_data.get("steamAppId", False))
        try:
            if isinstance(steam_app_id, str):
                steam_app_id_int = int(steam_app_id)
            else:
                raise ValueError
        except ValueError:
            if not steam_app_id:
                logger.warning(
                    f"Could not find steamAppId in mod data. Treating {mod.package_id} as a normal mod."
                )
            else:
                logger.warning(
                    f"Found steamAppId '{steam_app_id}' was not a valid integer. Treating {mod.package_id} as a normal mod."
                )
        else:
            mod = LudeonMod(**vars(mod), steam_app_id=steam_app_id_int)
            logger.info(
                f"Found steam app id '{mod.steam_app_id}' for suspected ludeon mod '{mod.package_id}'. Treating as Ludeon mod."
            )

            mod.description = "Unknown Ludeon mod"
            mod.name = "Unknown Ludeon mod"

    name = value_extractor(mod_data.get("name", False))
    if isinstance(name, str):
        mod.name = name
    elif not isinstance(mod, LudeonMod):
        message = "Couldn't parse a valid name. This mod may be be considered invalid by RimWorld."
        # If package id was valid string, default to that as name for display purposes
        if isinstance(package_id, str):
            mod.name = mod.package_id
            message += f" Defaulting to packageId: {package_id}"

        _set_mod_invalid(mod, message)

    description = value_extractor(mod_data.get("description", False))
    if isinstance(description, str):
        mod.description = description
    elif not isinstance(mod, LudeonMod):
        _set_mod_invalid(
            mod,
            "Couldn't parse a valid description. This mod may be be considered invalid by RimWorld.",
        )

    author = value_extractor(mod_data.get("author", False))
    authors = value_extractor(mod_data.get("authors", False))

    if isinstance(author, str):
        mod.authors.append(author)

    if authors:
        mod.authors.extend(authors)

    if (not (author or authors)) and not isinstance(mod, LudeonMod):
        _set_mod_invalid(
            mod, "Couldn't parse valid author(s). This mod may be invalid."
        )

    supported_versions = value_extractor(mod_data.get("supportedVersions", False))
    if isinstance(supported_versions, list):
        mod.supported_versions = set(supported_versions)
    elif isinstance(supported_versions, str):
        mod.supported_versions = {supported_versions}
    elif not isinstance(mod, LudeonMod):
        _set_mod_invalid(
            mod,
            "Couldn't parse valid supportedVersions. This mod may be invalid.",
        )

    return mod


def _parse_optional(
    mod_data: dict[str, Any], mod: RuledMod, target_version: str
) -> RuledMod:
    """
    Parse the optional fields from the mod_data and set them on the mod object.
    """

    mod_version = value_extractor(mod_data.get("modVersion", False))
    if mod_version and isinstance(mod_version, str):
        mod.mod_version = mod_version

    mod_icon_path = value_extractor(mod_data.get("modIconPath", False))
    if mod_icon_path and isinstance(mod_icon_path, str):
        mod.mod_icon_path = Path(mod_icon_path)

    url = value_extractor(mod_data.get("url", False))
    if url and isinstance(url, str):
        mod.url = url

    # Skip descriptionsByVersion

    mod.about_rules = create_base_rules(mod_data, target_version)

    raise NotImplementedError


def create_base_rules(
    mod_data: dict[str, Any], target_version: str
) -> BaseRules | Rules:
    rules = BaseRules()

    # Dependencies
    mod_dependencies = value_extractor(mod_data.get("modDependencies", []))
    mod_dependencies = (
        mod_dependencies if isinstance(mod_dependencies, list) else [mod_dependencies]
    )
    versioned_mod_dependencies = value_extractor(
        mod_data.get("modDependenciesByVersion", {})
    )

    if isinstance(versioned_mod_dependencies, dict):
        _, dependencies = match_version(versioned_mod_dependencies, target_version)
        if dependencies:
            mod_dependencies.extend(dependencies)

    for dependency in mod_dependencies:
        if isinstance(dependency, dict):
            dep = create_mod_dependency(dependency)

            if dep.package_id in rules.dependencies:
                logger.warning(
                    f"Duplicate dependency found: {dep.package_id}. Skipping."
                )
            else:
                rules.dependencies[dep.package_id] = dep
        else:
            logger.warning(
                f"Skipping invalid dependency: {dependency}. This mod may be invalid."
            )

    def load_operations(
        mod_data: dict[str, Any], key: str, force_key: str, target_version: str
    ) -> CaseInsensitiveSet:
        load = value_extractor(mod_data.get(key, []))
        load = load if isinstance(load, list) else [load]

        loadByVersion = value_extractor(mod_data.get(f"{key}ByVersion", {}))
        if isinstance(loadByVersion, dict):
            _, load_versioned = match_version(loadByVersion, target_version)
            if load_versioned:
                load.extend(load_versioned)

        forceLoad = value_extractor(mod_data.get(force_key, []))
        forceLoad = forceLoad if isinstance(forceLoad, list) else [forceLoad]
        load.extend(forceLoad)

        return CaseInsensitiveSet(load)

    # Load Before
    rules.load_before = load_operations(
        mod_data, "loadBefore", "forceLoadBefore", target_version
    )

    # Load After
    rules.load_after = load_operations(
        mod_data, "loadAfter", "forceLoadAfter", target_version
    )

    # incompatibleWith
    incompatible_with = value_extractor(mod_data.get("incompatibleWith", []))
    incompatible_with = (
        incompatible_with
        if isinstance(incompatible_with, list)
        else [incompatible_with]
    )

    incompatible_withByVersion = value_extractor(
        mod_data.get("incompatibleWithByVersion", {})
    )
    if isinstance(incompatible_withByVersion, dict):
        _, incompatibles = match_version(incompatible_withByVersion, target_version)
        if incompatibles:
            incompatible_with.extend(incompatibles)

    rules.incompatible_with = CaseInsensitiveSet(incompatible_with)

    return rules


def create_mod_dependency(input_dict: dict[str, str]) -> DependencyMod:
    """
    Create a DependencyMod object from the input dictionary.

    :param input_dict: The dictionary containing the mod data.
    :return: The DependencyMod object.
    """
    mod = DependencyMod()
    package_id = input_dict.get("packageId", False)
    if isinstance(package_id, str):
        mod.package_id = CaseInsensitiveStr(package_id)

    name = input_dict.get("displayName", False)
    if isinstance(name, str):
        mod.name = name

    workshop_url = input_dict.get("workshopUrl", False)
    if isinstance(workshop_url, str):
        mod.workshop_url = workshop_url

    return mod


def create_listed_mod_from_xml(
    mod_xml_path: str, target_version: str
) -> tuple[bool, ListedMod]:
    try:
        mod_data = xml_path_to_json(mod_xml_path)["ModMetaData"]
    except Exception:
        logger.error(
            f"Unable to parse {mod_xml_path} with the exception: {traceback.format_exc()}"
        )
        return False, ListedMod(valid=False)

    if not mod_data:
        logger.error(f"Could not parse {mod_xml_path}.")
        return False, ListedMod(valid=False)

    valid, mod = create_listed_mod(mod_data, target_version)

    mod.mod_path = Path(mod_xml_path)

    return valid, mod


@cache
def get_dlc_packageid_appid_map() -> dict[str, str]:
    return {dlc["packageid"]: appid for appid, dlc in RIMWORLD_DLC_METADATA.items()}


def get_rules_db(
    path: Path,
) -> ExternalRulesSchema | None:
    logger.info(f"Checking Rules DB at: {path}")
    if os.path.exists(
        path
    ):  # Look for cached data & load it if available
        logger.info(
            "DB exists!",
        )
        with open(path, encoding="utf-8") as f:
            json_string = f.read()
            logger.info("Reading info from rules DB")
            rule_data = msgspec.json.decode(json_string, type=ExternalRulesSchema)
            total_entries = len(rule_data.rules)
            logger.info(f"Loaded {total_entries} additional rules")
            return rule_data
    else:  # Assume db_data_missing
        logger.warning("Rules DB not found at specified path.")
        return None
