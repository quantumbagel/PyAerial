import ruamel.yaml

import constants
from constants import *
import logging

# This is in-progress configuration validation code. Not currently used.

validator = logging.getLogger("pyaerial.validator")

configuration = constants.CONFIGURATION
def validate_category(category, name):
    print(category)
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
        for item in CONFIG_CAT_SAVE_METHODS:
            if category[CONFIG_CAT_SAVE][saving_category].startswith(item):
                valid_save_method = True
                break
                
        elif :
            validator.warning(
                f"category {name} does not have a valid parameter for saving rule {saving_category}."
                f" Valid parameters are {CONFIG_CAT_SAVE_METHOD_TYPES}. Ignoring this category...")
            return False
    return True

zones = {}
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
    zones.update({zone_name: zone})

    for level_name in zone[CONFIG_ZONES_LEVELS]:
        level = zone[CONFIG_ZONES_LEVELS][level_name]
        invalid = False
        if type(level[CONFIG_ZONES_LEVELS_CATEGORY]) is str and level[CONFIG_ZONES_LEVELS_CATEGORY] not in configuration[CONFIG_CATEGORIES].keys():
            validator.warning(f"{zone_name}/{level_name} triggering {level[CONFIG_ZONES_LEVELS_CATEGORY]},"
                              f" which does not exist. This level WILL NOT BE USED, although the zone will.")
            invalid = True
        if type(level[CONFIG_ZONES_LEVELS_CATEGORY]) is dict and not validate_category(level[CONFIG_ZONES_LEVELS_CATEGORY], f"subcategory of level {level_name}"):
            invalid = True


if not len(zones):
    validator.critical("There are no functional zones, so running is pointless.")
    raise ValueError
configuration[CONFIG_ZONES] = zones