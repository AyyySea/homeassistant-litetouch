"""LiteTouch load-based light entities."""
import logging
from homeassistant.components.light import (
    LightEntity, ColorMode, ATTR_BRIGHTNESS
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import (
    DOMAIN, CONF_MODULE, CONF_CHANNEL, CONF_NAME, CONF_LOADID,
    CONF_DRIVE_SCENE,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_module(m):
    return m.strip().upper().zfill(4)


def setup_platform(hass, config, add_entities, discovery_info=None):
    data = hass.data[DOMAIN]
    controller = data['controller']
    entities = []
    for cfg in data['loads']:
        module = _normalize_module(cfg[CONF_MODULE])
        entities.append(LiteTouchLoad(
            controller, module, cfg[CONF_CHANNEL],
            cfg[CONF_NAME], cfg.get(CONF_DRIVE_SCENE)
        ))
    for cfg in data['scenes']:
        entities.append(LiteTouchScene(
            controller, cfg[CONF_LOADID], cfg[CONF_NAME]
        ))
    add_entities(entities)


class LiteTouchLoad(LightEntity):
    """One physical load. Driven via CINLL on drive_scene, state from RMODU.

    drive_scene = None means read-only: state still tracks RMODU but
    turn_on/turn_off are no-ops (load is controlled only by keypads or
    aggregate scenes).
    """
    _attr_should_poll = False

    def __init__(self, controller, module, channel, name, drive_scene):
        self._controller = controller
        self._module = module
        self._channel = channel
        self._drive_scene = drive_scene
        self._attr_name = name
        self._attr_unique_id = f"litetouch_load_{module}_{channel}"
        self._level = 0  # LiteTouch native 0-100

        if drive_scene is None:
            # Read-only: still expose as light so HomeKit sees state,
            # but only ONOFF mode (no slider) since we can't drive it.
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
        else:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def is_on(self):
        return self._level > 0

    @property
    def brightness(self):
        if self._level <= 0:
            return 0
        # LiteTouch 0-100 -> HomeKit 0-255
        return min(255, max(1, int(self._level * 255 / 100)))

    async def async_added_to_hass(self):
        signal = f"litetouch_module_{self._module}"
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, self._handle_module_update)
        )

    @callback
    def _handle_module_update(self, levels):
        """levels: list of 8 ints. -1 = unchanged."""
        if 0 <= self._channel < len(levels):
            new_level = levels[self._channel]
            if new_level >= 0 and new_level != self._level:
                self._level = new_level
                self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        if self._drive_scene is None:
            _LOGGER.debug(
                "Load %s_%s is read-only (no drive_scene); ignoring turn_on",
                self._module, self._channel
            )
            return
        brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
        # HomeKit 0-255 -> LiteTouch 0-100. Any hardware CGMAX cap clamps further.
        level = max(1, min(100, int(brightness * 100 / 255)))
        await self.hass.async_add_executor_job(
            self._controller.set_scene_level, self._drive_scene, level
        )
        # State update will arrive via RMODU broadcast; don't overwrite optimistically

    async def async_turn_off(self, **kwargs):
        if self._drive_scene is None:
            _LOGGER.debug(
                "Load %s_%s is read-only (no drive_scene); ignoring turn_off",
                self._module, self._channel
            )
            return
        await self.hass.async_add_executor_job(
            self._controller.fire_scene_off, self._drive_scene
        )
        # State update via RMODU


class LiteTouchScene(LightEntity):
    """Aggregate scene fired via CSLON/CSLOF. Optimistic state."""
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, controller, loadid, name):
        self._controller = controller
        self._loadid = loadid
        self._attr_name = name
        self._attr_unique_id = f"litetouch_scene_{loadid}"
        self._on = False

    @property
    def is_on(self):
        return self._on

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(
            self._controller.fire_scene_on, self._loadid
        )
        self._on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            self._controller.fire_scene_off, self._loadid
        )
        self._on = False
        self.async_write_ha_state()
