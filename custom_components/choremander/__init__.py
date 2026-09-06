"""Choremander - Family Chore Manager for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started

from .const import (
    ATTR_ASSIGNED_TO,
    ATTR_CHILD_ID,
    ATTR_CHORE_ID,
    ATTR_CHORE_ORDER,
    ATTR_COMPLETION_PERCENTAGE_PER_MONTH,
    ATTR_COMPLETION_SOUND,
    ATTR_COST,
    ATTR_DATE_OVERRIDES,
    ATTR_DAY_OVERRIDES,
    ATTR_DAYS_TO_GOAL,
    ATTR_DESCRIPTION,
    ATTR_DUE_DAYS,
    ATTR_DAILY_LIMIT,
    ATTR_ICON,
    ATTR_IS_JACKPOT,
    ATTR_NAME,
    ATTR_OVERRIDE_POINT_VALUE,
    ATTR_POINTS,
    ATTR_REASON,
    ATTR_REQUIRES_APPROVAL,
    ATTR_REWARD_ID,
    ATTR_ROTATION,
    ATTR_ROTATION_ANCHOR,
    ATTR_ROTATION_ORDER,
    ATTR_TIME_CATEGORY,
    DOMAIN,
    SERVICE_ADD_CHORE,
    SERVICE_ADD_POINTS,
    SERVICE_ADD_REWARD,
    SERVICE_APPROVE_CHORE,
    SERVICE_APPROVE_REWARD,
    SERVICE_CLAIM_REWARD,
    SERVICE_COMPLETE_CHORE,
    SERVICE_DELETE_CHORE,
    SERVICE_DELETE_REWARD,
    SERVICE_REJECT_CHORE,
    SERVICE_REMOVE_POINTS,
    SERVICE_SET_CHORE_ORDER,
    SERVICE_SET_CHORE_SCHEDULE,
    SERVICE_UPDATE_CHORE,
    SERVICE_UPDATE_REWARD,
)
from .coordinator import ChoremanderCoordinator
from .frontend import async_register_cards, async_register_frontend
from .resource_repair import ISSUE_ID as RESOURCE_ISSUE_ID
from .resource_repair import async_check_for_deleted_resources

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON, Platform.BINARY_SENSOR]

# Track if services are registered
SERVICES_REGISTERED = "services_registered"

# Track if the deleted-resource check has been scheduled. Deliberately a
# top-level `hass.data` key rather than one inside `hass.data[DOMAIN]`: that
# dict is scanned entry-by-entry on unload, and every non-entry key left in it
# makes the last unload look like it still has entries (see the frontend
# sentinel and defect #1 in tests/unit/test_init.py).
RESOURCE_CHECK_SCHEDULED = f"{DOMAIN}_resource_check_scheduled"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Choremander from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ChoremanderCoordinator(hass, entry.entry_id)
    await coordinator.async_initialize()

    # Store initial settings from config entry
    if entry.data.get("points_name"):
        coordinator.storage.set_points_name(entry.data["points_name"])
    if entry.data.get("points_icon"):
        coordinator.storage.set_points_icon(entry.data["points_icon"])
    await coordinator.storage.async_save()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register frontend static paths and Lovelace resources (only once)
    await async_register_frontend(hass)
    await async_register_cards(hass)

    # Older versions truncated .storage/lovelace_resources, deleting resources
    # belonging to other integrations (issue #2). Look for the wreckage and
    # offer to put it back. Deferred until Home Assistant has started: it reads
    # every dashboard and scans config/www, neither of which should be on the
    # critical path of setting up an entry.
    if RESOURCE_CHECK_SCHEDULED not in hass.data:
        # Keep the unsub so an entry unloaded before Home Assistant finishes
        # starting does not leave a scan queued for an integration on its way
        # out. Safe to call after it has fired - it nulls itself.
        hass.data[RESOURCE_CHECK_SCHEDULED] = async_at_started(
            hass, async_check_for_deleted_resources
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (only once)
    if not hass.data[DOMAIN].get(SERVICES_REGISTERED):
        await _async_register_services(hass)
        hass.data[DOMAIN][SERVICES_REGISTERED] = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

        # If no more entries, unregister services
        remaining_entries = [
            key for key in hass.data[DOMAIN].keys() if key != SERVICES_REGISTERED
        ]
        if not remaining_entries:
            _async_unregister_services(hass)
            hass.data[DOMAIN][SERVICES_REGISTERED] = False

        _async_teardown_resource_check(hass)

    return unload_ok


@callback
def _async_teardown_resource_check(hass: HomeAssistant) -> None:
    """Cancel the pending deleted-resource check once the last entry is gone.

    Detected by looking for live coordinators rather than by the length of
    `hass.data[DOMAIN]`, which also holds non-entry sentinels and so never
    reaches zero once the frontend has registered.

    Deliberately does *not* touch the repair issue. A reload is an unload
    followed by a setup, and deleting the issue would throw away the
    `dismissed_version` the registry holds - so every options change would
    resurrect a repair the user had already dismissed. Removing the integration
    is handled by `async_remove_entry` instead.
    """
    if any(isinstance(value, ChoremanderCoordinator) for value in hass.data[DOMAIN].values()):
        return

    if (unsub := hass.data.pop(RESOURCE_CHECK_SCHEDULED, None)) is not None:
        unsub()


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up after the integration is removed, not merely unloaded.

    The repair issue must not outlive Choremander. Once the last entry is gone
    there is no repairs platform registered, so Home Assistant substitutes a
    plain confirm flow: the user's "Fix" click would silently dismiss the issue
    and restore nothing.

    Only when the *last* entry goes, though - while any remain the platform is
    still registered and the repair still works.
    """
    if hass.config_entries.async_entries(DOMAIN):
        return

    ir.async_delete_issue(hass, DOMAIN, RESOURCE_ISSUE_ID)


def _get_coordinator(hass: HomeAssistant) -> ChoremanderCoordinator | None:
    """Get the first available coordinator."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if key != SERVICES_REGISTERED and isinstance(value, ChoremanderCoordinator):
            return value
    return None


# The fields an update service may name, mapped onto the model attribute each
# one writes. The schedule columns are deliberately absent from the chore side:
# `async_set_chore_schedule` exists precisely so that an edit of one part of a
# chore never rewrites the schedule as a side effect, and an update service
# would undo that guarantee the moment it accepted `due_days`.
CHORE_UPDATE_FIELDS: dict[str, str] = {
    ATTR_NAME: "name",
    ATTR_DESCRIPTION: "description",
    ATTR_POINTS: "points",
    ATTR_ASSIGNED_TO: "assigned_to",
    ATTR_REQUIRES_APPROVAL: "requires_approval",
    ATTR_TIME_CATEGORY: "time_category",
    ATTR_DAILY_LIMIT: "daily_limit",
    ATTR_COMPLETION_SOUND: "completion_sound",
    ATTR_COMPLETION_PERCENTAGE_PER_MONTH: "completion_percentage_per_month",
}

REWARD_UPDATE_FIELDS: dict[str, str] = {
    ATTR_NAME: "name",
    ATTR_DESCRIPTION: "description",
    ATTR_COST: "cost",
    ATTR_ICON: "icon",
    ATTR_ASSIGNED_TO: "assigned_to",
    ATTR_IS_JACKPOT: "is_jackpot",
    ATTR_OVERRIDE_POINT_VALUE: "override_point_value",
    ATTR_DAYS_TO_GOAL: "days_to_goal",
}


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register Choremander services."""

    async def handle_complete_chore(call: ServiceCall) -> None:
        """Handle the complete_chore service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        chore_id = call.data[ATTR_CHORE_ID]
        child_id = call.data[ATTR_CHILD_ID]
        await coordinator.async_complete_chore(chore_id, child_id)

    async def handle_approve_chore(call: ServiceCall) -> None:
        """Handle the approve_chore service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        completion_id = call.data["completion_id"]
        await coordinator.async_approve_chore(completion_id)

    async def handle_reject_chore(call: ServiceCall) -> None:
        """Handle the reject_chore service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        completion_id = call.data["completion_id"]
        await coordinator.async_reject_chore(completion_id)

    async def handle_claim_reward(call: ServiceCall) -> None:
        """Handle the claim_reward service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        reward_id = call.data[ATTR_REWARD_ID]
        child_id = call.data[ATTR_CHILD_ID]
        await coordinator.async_claim_reward(reward_id, child_id)

    async def handle_approve_reward(call: ServiceCall) -> None:
        """Handle the approve_reward service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        claim_id = call.data["claim_id"]
        await coordinator.async_approve_reward(claim_id)

    async def handle_add_points(call: ServiceCall) -> None:
        """Handle the add_points service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        child_id = call.data[ATTR_CHILD_ID]
        points = call.data[ATTR_POINTS]
        reason = call.data.get(ATTR_REASON, "")
        await coordinator.async_add_points(child_id, points, reason)

    async def handle_remove_points(call: ServiceCall) -> None:
        """Handle the remove_points service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        child_id = call.data[ATTR_CHILD_ID]
        points = call.data[ATTR_POINTS]
        reason = call.data.get(ATTR_REASON, "")
        await coordinator.async_remove_points(child_id, points, reason)

    async def handle_set_chore_order(call: ServiceCall) -> None:
        """Handle the set_chore_order service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        child_id = call.data[ATTR_CHILD_ID]
        chore_order = call.data[ATTR_CHORE_ORDER]
        await coordinator.async_set_chore_order(child_id, chore_order)

    async def handle_set_chore_schedule(call: ServiceCall) -> None:
        """Handle the set_chore_schedule service call.

        Only the fields actually present in the call are forwarded. A schedule
        is edited one part at a time - the card writes back a single row of the
        grid - so a call that moves Saturday must leave a rotation set last week
        alone. Passing `None` for an absent field would be indistinguishable
        from being asked to clear it, which is why this reads `in call.data`
        instead of using defaults.
        """
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        fields = {
            name: call.data[name]
            for name in (
                ATTR_DUE_DAYS,
                ATTR_ROTATION,
                ATTR_ROTATION_ORDER,
                ATTR_ROTATION_ANCHOR,
                ATTR_DAY_OVERRIDES,
                ATTR_DATE_OVERRIDES,
            )
            if name in call.data
        }
        await coordinator.async_set_chore_schedule(call.data[ATTR_CHORE_ID], **fields)

    async def handle_add_chore(call: ServiceCall) -> None:
        """Handle the add_chore service call.

        The core fields go through `async_add_chore`, which owns the defaults.
        Schedule fields, if any were sent, are written in a second step through
        `async_set_chore_schedule` - the same code path the schedule card uses -
        so a chore created with a rotation lands in storage exactly like a
        rotation added later would. Absent schedule fields mean "no schedule",
        which is what a fresh chore already carries, so there is nothing to
        distinguish here.
        """
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        chore = await coordinator.async_add_chore(
            name=call.data[ATTR_NAME],
            points=call.data.get(ATTR_POINTS, 10),
            description=call.data.get(ATTR_DESCRIPTION, ""),
            assigned_to=call.data.get(ATTR_ASSIGNED_TO),
            requires_approval=call.data.get(ATTR_REQUIRES_APPROVAL, True),
            time_category=call.data.get(ATTR_TIME_CATEGORY, "anytime"),
            daily_limit=call.data.get(ATTR_DAILY_LIMIT, 1),
            completion_sound=call.data.get(ATTR_COMPLETION_SOUND, "coin"),
            completion_percentage_per_month=call.data.get(
                ATTR_COMPLETION_PERCENTAGE_PER_MONTH, 100
            ),
        )
        schedule_fields = {
            name: call.data[name]
            for name in (
                ATTR_DUE_DAYS,
                ATTR_ROTATION,
                ATTR_ROTATION_ORDER,
                ATTR_ROTATION_ANCHOR,
                ATTR_DAY_OVERRIDES,
                ATTR_DATE_OVERRIDES,
            )
            if name in call.data
        }
        if schedule_fields:
            await coordinator.async_set_chore_schedule(chore.id, **schedule_fields)

    async def handle_update_chore(call: ServiceCall) -> None:
        """Handle the update_chore service call.

        Only the fields actually present in the call are applied, so a call
        that changes the points leaves the description and the schedule alone.
        Renaming is just `name`. An unknown chore id raises rather than
        creating a ghost row - `storage.update_chore` upserts, and a typo in an
        automation silently adding a chore named "chore_x" is not a price
        worth paying for the convenience.
        """
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        chore = coordinator.get_chore(call.data[ATTR_CHORE_ID])
        if not chore:
            raise ValueError(f"Chore {call.data[ATTR_CHORE_ID]} not found")
        for attr, field in CHORE_UPDATE_FIELDS.items():
            if attr in call.data:
                setattr(chore, field, call.data[attr])
        await coordinator.async_update_chore(chore)

    async def handle_delete_chore(call: ServiceCall) -> None:
        """Handle the delete_chore service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        await coordinator.async_remove_chore(call.data[ATTR_CHORE_ID])

    async def handle_add_reward(call: ServiceCall) -> None:
        """Handle the add_reward service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        await coordinator.async_add_reward(
            name=call.data[ATTR_NAME],
            cost=call.data.get(ATTR_COST, 50),
            description=call.data.get(ATTR_DESCRIPTION, ""),
            icon=call.data.get(ATTR_ICON, "mdi:gift"),
            assigned_to=call.data.get(ATTR_ASSIGNED_TO),
            is_jackpot=call.data.get(ATTR_IS_JACKPOT, False),
            override_point_value=call.data.get(ATTR_OVERRIDE_POINT_VALUE, False),
            days_to_goal=call.data.get(ATTR_DAYS_TO_GOAL, 30),
        )

    async def handle_update_reward(call: ServiceCall) -> None:
        """Handle the update_reward service call.

        Partial update over the named fields, rename via `name`; an unknown
        reward id raises rather than upserting, for the same reason
        `update_chore` does.
        """
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        reward = coordinator.get_reward(call.data[ATTR_REWARD_ID])
        if not reward:
            raise ValueError(f"Reward {call.data[ATTR_REWARD_ID]} not found")
        for attr, field in REWARD_UPDATE_FIELDS.items():
            if attr in call.data:
                setattr(reward, field, call.data[attr])
        await coordinator.async_update_reward(reward)

    async def handle_delete_reward(call: ServiceCall) -> None:
        """Handle the delete_reward service call."""
        coordinator = _get_coordinator(hass)
        if not coordinator:
            _LOGGER.error("No Choremander coordinator available")
            return
        await coordinator.async_remove_reward(call.data[ATTR_REWARD_ID])

    # Register all services
    hass.services.async_register(
        DOMAIN,
        SERVICE_COMPLETE_CHORE,
        handle_complete_chore,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHORE_ID): cv.string,
                vol.Required(ATTR_CHILD_ID): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPROVE_CHORE,
        handle_approve_chore,
        schema=vol.Schema(
            {
                vol.Required("completion_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REJECT_CHORE,
        handle_reject_chore,
        schema=vol.Schema(
            {
                vol.Required("completion_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLAIM_REWARD,
        handle_claim_reward,
        schema=vol.Schema(
            {
                vol.Required(ATTR_REWARD_ID): cv.string,
                vol.Required(ATTR_CHILD_ID): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPROVE_REWARD,
        handle_approve_reward,
        schema=vol.Schema(
            {
                vol.Required("claim_id"): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_POINTS,
        handle_add_points,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHILD_ID): cv.string,
                vol.Required(ATTR_POINTS): cv.positive_int,
                vol.Optional(ATTR_REASON, default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_POINTS,
        handle_remove_points,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHILD_ID): cv.string,
                vol.Required(ATTR_POINTS): cv.positive_int,
                vol.Optional(ATTR_REASON, default=""): cv.string,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHORE_ORDER,
        handle_set_chore_order,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHILD_ID): cv.string,
                vol.Required(ATTR_CHORE_ORDER): vol.All(cv.ensure_list, [cv.string]),
            }
        ),
    )

    # Every schedule field is optional, and none carries a default: the
    # coordinator distinguishes "not mentioned" from "cleared", and a default
    # would collapse the two. Nothing validates the *values* either - an
    # unknown rotation mode or a weekday that is not a weekday is stored as
    # sent, because schedule.py already degrades gracefully on all of them and
    # a second set of rules here would be one more thing to keep in step.
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHORE_SCHEDULE,
        handle_set_chore_schedule,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHORE_ID): cv.string,
                vol.Optional(ATTR_DUE_DAYS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_ROTATION): cv.string,
                vol.Optional(ATTR_ROTATION_ORDER): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_ROTATION_ANCHOR): cv.string,
                vol.Optional(ATTR_DAY_OVERRIDES): {cv.string: vol.All(cv.ensure_list, [cv.string])},
                vol.Optional(ATTR_DATE_OVERRIDES): {
                    cv.string: vol.All(cv.ensure_list, [cv.string])
                },
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_CHORE,
        handle_add_chore,
        schema=vol.Schema(
            {
                vol.Required(ATTR_NAME): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(ATTR_POINTS): cv.positive_int,
                vol.Optional(ATTR_DESCRIPTION): cv.string,
                vol.Optional(ATTR_ASSIGNED_TO): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_REQUIRES_APPROVAL): cv.boolean,
                vol.Optional(ATTR_TIME_CATEGORY): cv.string,
                vol.Optional(ATTR_DAILY_LIMIT): cv.positive_int,
                vol.Optional(ATTR_COMPLETION_SOUND): cv.string,
                vol.Optional(ATTR_COMPLETION_PERCENTAGE_PER_MONTH): cv.positive_int,
                vol.Optional(ATTR_DUE_DAYS): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_ROTATION): cv.string,
                vol.Optional(ATTR_ROTATION_ORDER): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_ROTATION_ANCHOR): cv.string,
                vol.Optional(ATTR_DAY_OVERRIDES): {cv.string: vol.All(cv.ensure_list, [cv.string])},
                vol.Optional(ATTR_DATE_OVERRIDES): {
                    cv.string: vol.All(cv.ensure_list, [cv.string])
                },
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CHORE,
        handle_update_chore,
        schema=vol.Schema(
            {
                vol.Required(ATTR_CHORE_ID): cv.string,
                vol.Optional(ATTR_NAME): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(ATTR_DESCRIPTION): cv.string,
                vol.Optional(ATTR_POINTS): cv.positive_int,
                vol.Optional(ATTR_ASSIGNED_TO): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_REQUIRES_APPROVAL): cv.boolean,
                vol.Optional(ATTR_TIME_CATEGORY): cv.string,
                vol.Optional(ATTR_DAILY_LIMIT): cv.positive_int,
                vol.Optional(ATTR_COMPLETION_SOUND): cv.string,
                vol.Optional(ATTR_COMPLETION_PERCENTAGE_PER_MONTH): cv.positive_int,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_CHORE,
        handle_delete_chore,
        schema=vol.Schema({vol.Required(ATTR_CHORE_ID): cv.string}),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_REWARD,
        handle_add_reward,
        schema=vol.Schema(
            {
                vol.Required(ATTR_NAME): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(ATTR_COST): cv.positive_int,
                vol.Optional(ATTR_DESCRIPTION): cv.string,
                vol.Optional(ATTR_ICON): cv.string,
                vol.Optional(ATTR_ASSIGNED_TO): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_IS_JACKPOT): cv.boolean,
                vol.Optional(ATTR_OVERRIDE_POINT_VALUE): cv.boolean,
                vol.Optional(ATTR_DAYS_TO_GOAL): cv.positive_int,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_REWARD,
        handle_update_reward,
        schema=vol.Schema(
            {
                vol.Required(ATTR_REWARD_ID): cv.string,
                vol.Optional(ATTR_NAME): vol.All(cv.string, vol.Length(min=1)),
                vol.Optional(ATTR_DESCRIPTION): cv.string,
                vol.Optional(ATTR_COST): cv.positive_int,
                vol.Optional(ATTR_ICON): cv.string,
                vol.Optional(ATTR_ASSIGNED_TO): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_IS_JACKPOT): cv.boolean,
                vol.Optional(ATTR_OVERRIDE_POINT_VALUE): cv.boolean,
                vol.Optional(ATTR_DAYS_TO_GOAL): cv.positive_int,
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_REWARD,
        handle_delete_reward,
        schema=vol.Schema({vol.Required(ATTR_REWARD_ID): cv.string}),
    )


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister Choremander services."""
    services = [
        SERVICE_COMPLETE_CHORE,
        SERVICE_APPROVE_CHORE,
        SERVICE_REJECT_CHORE,
        SERVICE_CLAIM_REWARD,
        SERVICE_APPROVE_REWARD,
        SERVICE_ADD_POINTS,
        SERVICE_REMOVE_POINTS,
        SERVICE_SET_CHORE_ORDER,
        SERVICE_SET_CHORE_SCHEDULE,
        SERVICE_ADD_CHORE,
        SERVICE_UPDATE_CHORE,
        SERVICE_DELETE_CHORE,
        SERVICE_ADD_REWARD,
        SERVICE_UPDATE_REWARD,
        SERVICE_DELETE_REWARD,
    ]
    for service in services:
        hass.services.async_remove(DOMAIN, service)
