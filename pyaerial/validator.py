"""
Configuration validation (schema aligned with runtime and README).
"""
from __future__ import annotations

import logging

from pyaerial.constants import (
    CONFIG_CAT_ALERT_ARGUMENTS,
    CONFIG_CAT_ALERT_METHODS,
    CONFIG_CAT_DEFAULT_SAVE_METHOD,
    CONFIG_CAT_METHOD,
    CONFIG_CAT_SAVE,
    CONFIG_CAT_SAVE_CALCULATED,
    CONFIG_CAT_SAVE_METHODS,
    CONFIG_CAT_SAVE_TELEMETRY,
    CONFIG_COMPONENTS,
    CONFIG_COMP_CTYPES,
    CONFIG_COMP_TYPES,
    CONFIG_GENERAL,
    CONFIG_GENERAL_ADVANCED_STATUS,
    CONFIG_GENERAL_BACKDATE,
    CONFIG_GENERAL_HERTZ,
    CONFIG_GENERAL_LOGGING_LEVEL,
    CONFIG_GENERAL_MERGE_PACKETS,
    CONFIG_GENERAL_MONGODB,
    CONFIG_GENERAL_REMEMBER,
    CONFIG_GENERAL_TOP_PLANES,
    CONFIG_HOME,
    CONFIG_HOME_LATITUDE,
    CONFIG_HOME_LONGITUDE,
    CONFIG_RECV_ARGUMENTS,
    CONFIG_RECV_METHOD,
    CONFIG_RECV_METHODS,
    CONFIG_RECEIVERS,
    CONFIG_ZONES,
    CONFIG_ZONES_COORDINATES,
    CONFIG_ZONES_LEVELS,
    CONFIG_ZONES_LEVELS_CATEGORY,
    CONFIG_ZONES_LEVELS_REQUIREMENTS,
    CONFIG_ZONES_LEVELS_SECONDS,
    CONFIG_CATEGORIES,
    KAFKA_METHOD_ARGUMENT_SERVER,
    LOGGING_LEVELS,
    STORE_CALC_DATA,
    STORE_DATA_TYPES,
    STORE_RECV_DATA,
)
from pyaerial.requirements_eval import RequirementError, collect_component_names, eval_requirement

log = logging.getLogger("pyaerial.validator")

_SAVE_SECTION_TO_STORE = {
    CONFIG_CAT_SAVE_TELEMETRY: STORE_RECV_DATA,
    CONFIG_CAT_SAVE_CALCULATED: STORE_CALC_DATA,
}


def _validate_save_method_string(method: str, context: str) -> bool:
    if not isinstance(method, str):
        log.warning("%s: save method must be a string, got %s", context, type(method).__name__)
        return False
    for item, arg_count in CONFIG_CAT_SAVE_METHODS.items():
        if arg_count == 0 and method == item:
            return True
        if arg_count > 0 and method.startswith(item):
            rest = method.replace(item, "", 1).replace(" ", "").strip()
            if not (rest.startswith("(") and rest.endswith(")")):
                log.warning("%s: invalid %s syntax: %s", context, item, method)
                return False
            inner = [x.strip() for x in rest[1:-1].split(",") if x.strip()]
            if len(inner) != arg_count:
                log.warning("%s: %s expects %s args in %s", context, item, arg_count, method)
                return False
            for a in inner:
                if not a.replace(".", "", 1).isdigit():
                    log.warning("%s: non-numeric argument in %s", context, method)
                    return False
            return True
    log.warning("%s: unknown save method %s", context, method)
    return False


def _validate_category(name: str, category: dict) -> bool:
    ok = True
    if CONFIG_CAT_METHOD not in category:
        log.warning('Category "%s" missing "%s"', name, CONFIG_CAT_METHOD)
        return False
    method = category[CONFIG_CAT_METHOD]
    if method not in CONFIG_CAT_ALERT_METHODS:
        log.warning('Category "%s" has invalid alert method %s', name, method)
        ok = False
    elif CONFIG_CAT_ALERT_ARGUMENTS in category:
        args = category[CONFIG_CAT_ALERT_ARGUMENTS]
        for req in CONFIG_CAT_ALERT_METHODS[method]:
            if req not in args:
                log.warning('Category "%s" method %s missing argument %s', name, method, req)
                ok = False
            elif req == KAFKA_METHOD_ARGUMENT_SERVER and not args[req]:
                log.warning('Category "%s": kafka server argument empty', name)
                ok = False
    if CONFIG_CAT_SAVE not in category:
        log.warning('Category "%s" missing "%s"', name, CONFIG_CAT_SAVE)
        return False
    save = category[CONFIG_CAT_SAVE]
    if not isinstance(save, dict):
        log.warning('Category "%s" save must be a mapping', name)
        return False

    for section, store_key in _SAVE_SECTION_TO_STORE.items():
        if section not in save:
            log.warning('Category "%s" missing save.%s', name, section)
            ok = False
            continue
        block = save[section]
        if not isinstance(block, dict):
            log.warning('Category "%s" save.%s must be a mapping', name, section)
            ok = False
            continue
        if CONFIG_CAT_DEFAULT_SAVE_METHOD not in block:
            log.warning('Category "%s" save.%s missing "default"', name, section)
            ok = False
        elif not _validate_save_method_string(
            block[CONFIG_CAT_DEFAULT_SAVE_METHOD], f'category "{name}" save.{section}.default'
        ):
            ok = False
        allowed = set(STORE_DATA_TYPES[store_key])
        for key, val in block.items():
            if key == CONFIG_CAT_DEFAULT_SAVE_METHOD:
                continue
            if key not in allowed:
                log.warning('Category "%s" save.%s unknown field %s', name, section, key)
                ok = False
            elif not _validate_save_method_string(val, f'category "{name}" save.{section}.{key}'):
                ok = False
    return ok


def _validate_components(configuration: dict) -> bool:
    ok = True
    comps = configuration.get(CONFIG_COMPONENTS, {})
    if not isinstance(comps, dict):
        log.warning('"components" must be a mapping')
        return False
    for cname, spec in comps.items():
        if not isinstance(spec, dict):
            log.warning('Component "%s" must be a mapping', cname)
            ok = False
            continue
        for dtype, rules in spec.items():
            if dtype not in CONFIG_COMP_TYPES:
                log.warning('Component "%s" unknown data type %s', cname, dtype)
                ok = False
                continue
            if not isinstance(rules, dict):
                log.warning('Component "%s" field %s must be a mapping of comparisons', cname, dtype)
                ok = False
                continue
            for ctype, limit in rules.items():
                if ctype not in CONFIG_COMP_CTYPES:
                    log.warning('Component "%s" %s invalid comparison %s', cname, dtype, ctype)
                    ok = False
                elif dtype not in CONFIG_COMP_CTYPES[ctype]:
                    log.warning('Component "%s" cannot use %s on %s', cname, ctype, dtype)
                    ok = False
    return ok


def _validate_requirement_expr(expr: str, configuration: dict) -> bool:
    try:
        names = collect_component_names(expr)
    except SyntaxError as e:
        log.warning("Invalid requirement expression syntax: %s (%s)", expr, e)
        return False
    comps = configuration.get(CONFIG_COMPONENTS, {})
    for n in names:
        if n not in comps:
            log.warning('Requirement references unknown component "%s"', n)
            return False
    env = {n: True for n in names}
    try:
        eval_requirement(expr, env)
    except RequirementError as e:
        log.warning("Requirement expression invalid: %s (%s)", expr, e)
        return False
    return True


def validate_config(configuration: dict) -> list[tuple[str, str]]:
    """
    Validate configuration. Returns a list of (severity, message) where severity is
    'error' or 'warning'. Caller may exit on errors.
    """
    issues: list[tuple[str, str]] = []

    def err(msg: str):
        issues.append(("error", msg))
        log.error(msg)

    def warn(msg: str):
        issues.append(("warning", msg))
        log.warning(msg)

    for key in (CONFIG_GENERAL, CONFIG_HOME, CONFIG_RECEIVERS, CONFIG_ZONES, CONFIG_COMPONENTS):
        if key not in configuration:
            err(f'Missing required top-level key "{key}"')

    if any(s == "error" for s, _ in issues):
        return issues

    gen = configuration[CONFIG_GENERAL]
    for k in (
        CONFIG_GENERAL_MONGODB,
        CONFIG_GENERAL_BACKDATE,
        CONFIG_GENERAL_REMEMBER,
        CONFIG_GENERAL_TOP_PLANES,
        CONFIG_GENERAL_ADVANCED_STATUS,
        CONFIG_GENERAL_HERTZ,
        CONFIG_GENERAL_LOGGING_LEVEL,
        CONFIG_GENERAL_MERGE_PACKETS,
    ):
        if k not in gen:
            err(f'general.{k} is required')

    if gen.get(CONFIG_GENERAL_LOGGING_LEVEL) not in LOGGING_LEVELS:
        err(f'general.logs must be one of {list(LOGGING_LEVELS.keys())}')

    home = configuration[CONFIG_HOME]
    for k in (CONFIG_HOME_LATITUDE, CONFIG_HOME_LONGITUDE):
        if k not in home:
            err(f'home.{k} is required')

    receivers = configuration[CONFIG_RECEIVERS]
    if not isinstance(receivers, dict) or not receivers:
        err("At least one receiver is required under 'receivers'")
    else:
        for rname, rspec in receivers.items():
            if CONFIG_RECV_METHOD not in rspec:
                err(f'Receiver "{rname}" missing method')
                continue
            m = rspec[CONFIG_RECV_METHOD]
            if m not in CONFIG_RECV_METHODS:
                err(f'Receiver "{rname}" unknown method {m}')
                continue
            args = rspec.get(CONFIG_RECV_ARGUMENTS, {})
            for arg_name in CONFIG_RECV_METHODS[m]:
                if arg_name not in args:
                    err(f'Receiver "{rname}" missing argument {arg_name}')

    if not _validate_components(configuration):
        warn("One or more components have issues (see logs above)")

    cats = configuration.get(CONFIG_CATEGORIES, {})
    if not isinstance(cats, dict) or not cats:
        warn("No categories defined")
    else:
        for cname, cat in cats.items():
            if not isinstance(cat, dict):
                err(f'Category "{cname}" must be a mapping')
            elif not _validate_category(cname, cat):
                warn(f'Category "{cname}" has validation issues')

    zones = configuration[CONFIG_ZONES]
    if not isinstance(zones, dict) or not zones:
        err("At least one zone is required")
    else:
        for zname, zone in zones.items():
            if CONFIG_ZONES_COORDINATES not in zone:
                err(f'Zone "{zname}" missing coordinates')
            elif not isinstance(zone[CONFIG_ZONES_COORDINATES], list):
                err(f'Zone "{zname}" coordinates must be a list')
            elif len(zone[CONFIG_ZONES_COORDINATES]) < 3:
                warn(f'Zone "{zname}" should have at least 3 vertices')
            levels = zone.get(CONFIG_ZONES_LEVELS, {})
            if not isinstance(levels, dict) or not levels:
                err(f'Zone "{zname}" needs levels')
            for lname, level in levels.items():
                for fld in (
                    CONFIG_ZONES_LEVELS_CATEGORY,
                    CONFIG_ZONES_LEVELS_REQUIREMENTS,
                    CONFIG_ZONES_LEVELS_SECONDS,
                ):
                    if fld not in level:
                        err(f'Zone "{zname}" level "{lname}" missing {fld}')
                cat = level.get(CONFIG_ZONES_LEVELS_CATEGORY)
                if isinstance(cat, str) and cat not in cats:
                    err(f'Zone "{zname}" level "{lname}" references unknown category "{cat}"')
                if CONFIG_ZONES_LEVELS_REQUIREMENTS in level:
                    if not _validate_requirement_expr(level[CONFIG_ZONES_LEVELS_REQUIREMENTS], configuration):
                        warn(f'Zone "{zname}" level "{lname}" requirement expression failed validation')
                sec = level.get(CONFIG_ZONES_LEVELS_SECONDS)
                if sec is not None and (not isinstance(sec, int) or sec < 1):
                    warn(f'Zone "{zname}" level "{lname}" seconds should be int >= 1')

    return issues
