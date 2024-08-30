import ast

import ruamel.yaml

import constants
from constants import *
import logging

# This is in-progress configuration validation code. Not currently used.

validator = logging.getLogger("pyaerial.validator")

# configuration = constants.CONFIGURATION

yaml = ruamel.yaml.YAML(typ='safe')
with open(CONFIG_FILE) as config:
    configuration = yaml.load(config)

def validate_requirements(requirements, level_name):
    return True


def validate_category(category, name):
    if CONFIG_CAT_METHOD not in category.keys():
        validator.warning(f"category {name} does not have any defined alert method. The category won't be used.")
    if category[CONFIG_CAT_METHOD] not in CONFIG_CAT_ALERT_METHODS:
        validator.warning(f"category {name} does not have a valid alert method. Valid methods: {CONFIG_CAT_ALERT_METHODS.keys()}. It will not be used.")
        return False
    elif CONFIG_CAT_SAVE not in category.keys():
        validator.warning(
            f"category {name} does not have saving rules. Ignoring...")
        return False
    for saving_category in CONFIG_CAT_SAVE_METHOD_TYPES:
        if saving_category not in category[CONFIG_CAT_SAVE]:
            validator.warning(
                f"category {name} does not have saving rule {saving_category}."
                f" Ignoring this category...")
            return False
        valid_save_method = False

        for item in CONFIG_CAT_SAVE_METHODS.keys():
            argument_number = CONFIG_CAT_SAVE_METHODS[item]
            saving_method = category[CONFIG_CAT_SAVE][saving_category]
            if not argument_number:

                if saving_method == item:
                    valid_save_method = True
                    break
            else:
                if saving_method.startswith(item):
                    after_parenthesis = saving_method.replace(" ", "").split("(")
                    if len(after_parenthesis) == 1:
                        validator.warning(f"argument type {item} requires {argument_number} argument(s), didn't find argument formatting. Example: decimate(10), sdecimate(1,10) or sdecimate(1, 10)")
                        break
                    elif len(after_parenthesis) == 2:  # we need exactly 3
                        if after_parenthesis[1].count(")") != 1 or not after_parenthesis[1].endswith(")"):
                            validator.warning(f"Closing parentheses error: argument {saving_method} is not valid! Example: decimate(10), sdecimate(1,10) or sdecimate(1, 10)")
                            break

                        arguments = after_parenthesis[1].replace(")", "").split(',')
                        digit_fail = False
                        for argument in arguments:
                            if argument == "":
                                validator.warning(
                                    f"Trailing (or leading) comma detected. Please remove it. (in {saving_method} in category {name})")
                                digit_fail = True
                                break
                            if not argument.isdigit():
                                validator.warning(f"Invalid argument! I don't understand \"{argument}\" (in {saving_method} in category {name})")
                                digit_fail = True
                                break

                        if digit_fail:
                            break
                        parsed_arguments = [int(i) for i in arguments]
                        if len(parsed_arguments) != argument_number:
                            validator.warning(
                                f"argument type {item} requires {argument_number} argument(s), got {len(parsed_arguments)} arguments. Triggered by {saving_method} in {name}.")

                            break
                    else:
                        validator.warning(f"Invalid argument! I don't understand what {saving_method} means")
                        break
                    valid_save_method = True
        if not valid_save_method:
            validator.warning(f"Because method {saving_category} is invalid, {name} is invalid.")
            return False
    return True

def validate_level(level, level_name, zone_name):
    invalid = False

    # Existence checks
    if CONFIG_ZONES_LEVELS_CATEGORY not in level.keys():
        validator.warning(f"There is no category in level {level_name} in zone {zone_name}! We can't use it.")
        invalid = True
    elif CONFIG_ZONES_LEVELS_SECONDS not in level.keys():
        validator.warning(f"There is no \"seconds\" requirement in level {level_name} in zone {zone_name}! We can't use it.")
        invalid = True
    elif CONFIG_ZONES_LEVELS_REQUIREMENTS not in level.keys():
        validator.warning(f"There is no requirements in level {level_name} in zone {zone_name}! We can't use it.")
        invalid = True
    if invalid:
        return False

    category = level[CONFIG_ZONES_LEVELS_CATEGORY]
    requirement = level[CONFIG_ZONES_LEVELS_REQUIREMENTS]
    seconds = level[CONFIG_ZONES_LEVELS_SECONDS]

    # Category checks
    if type(category) is str and category not in configuration[CONFIG_CATEGORIES].keys():
        validator.warning(f"{level_name} in {zone_name} triggers category \"{category}\","
                          f" which does not exist. This level WILL NOT BE USED, although the zone will.")
        invalid = True
    elif type(category) is dict and not validate_category(category, f"subcategory of level {level_name}"):
        invalid = True
    elif type(category) is str and not validate_category(configuration[CONFIG_CATEGORIES][category], category):
        invalid = True
    if invalid:
        return False

    # Requirement checks
    component_names = [node.id for node in ast.walk(ast.parse(requirement))
                       if type(node) is ast.Name]

    for component in component_names:
        if component not in configuration[CONFIG_COMPONENTS].keys():
            invalid = True
            validator.warning(
                f"{level_name} in {zone_name} triggers requirement \"{component}\", which doesn't exist.")
        if not validate_requirements(component, level_name):
            invalid = True
    if invalid:
        return False

    # Seconds checks

    if type(seconds) is str and not seconds.isdigit():
        validator.warning(f"{level_name}'s \"seconds\" (in {zone_name}) requirement must be an integer!")
        invalid = True
    elif type(seconds) is int and seconds < 1:
        validator.warning(f"{level_name}'s \"seconds\" requirement (in {zone_name}) must be an integer 1 or higher.")
        invalid = True
    elif type(seconds) is float:
        validator.warning(f"{level_name}'s"
                          f" \"seconds\" requirement (in {zone_name}) must be an integer 1 or higher, not a float!")
        invalid = True
    if invalid:
        return False

    return True


def validate_zones():
    valid_zones = {}
    for zone_name in configuration[CONFIG_ZONES]:
        zone = configuration[CONFIG_ZONES][zone_name]
        invalid = False
        if CONFIG_ZONES_COORDINATES not in zone.keys():
            validator.warning(f"{zone_name} has no coordinates. This zone WILL NOT BE USED.")
            invalid = True
        if CONFIG_ZONES_LEVELS not in zone.keys():
            validator.warning(f"{zone_name} has no levels that it can trigger. This zone WILL NOT BE USED.")
            invalid = True

        if invalid:
            continue

        # We are OK, so add
        enabled_levels = {}

        for level_name in zone[CONFIG_ZONES_LEVELS]:
            if validate_level(zone[CONFIG_ZONES_LEVELS][level_name], level_name, zone_name):
                enabled_levels.update({level_name: zone[CONFIG_ZONES_LEVELS][level_name]})
        if enabled_levels != {}:
            valid_zones.update({zone_name: zone})
        else:
            validator.warning(f"All levels for the zone {zone_name} contain errors. This zone WILL NOT BE USED.")
    return valid_zones

print(validate_zones().keys())
#
#
# if not len(zones):
#     validator.critical("There are no functional zones, so running is pointless.")
#     raise ValueError
# configuration[CONFIG_ZONES] = zones