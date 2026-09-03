"""RenderDoc session manager - singleton managing capture file lifecycle."""

from __future__ import annotations

import os

from renderdoc_mcp.util import rd, make_error


class RenderDocSession:
    """Manages a single RenderDoc capture file's lifecycle."""

    def __init__(self):
        self._initialized: bool = False
        self._cap = None  # CaptureFile
        self._controller = None  # ReplayController
        self._remote = None  # RemoteServer, set when replaying via a remote host
        self._remote_host: str | None = None
        self._filepath: str | None = None
        self._current_event: int | None = None
        self._action_map: dict[int, object] = {}
        self._structured_file = None
        self._resource_id_cache: dict[str, object] = {}  # str(resourceId) → resourceId
        self._texture_desc_cache: dict[str, object] = {}  # str(resourceId) → TextureDescription

    def _ensure_initialized(self):
        """Initialize the replay system if not already done."""
        if not self._initialized:
            rd.InitialiseReplay(rd.GlobalEnvironment(), [])
            self._initialized = True

    @property
    def is_open(self) -> bool:
        return self._controller is not None

    @property
    def filepath(self) -> str | None:
        return self._filepath

    @property
    def remote_host(self) -> str | None:
        return self._remote_host

    @property
    def controller(self):
        return self._controller

    @property
    def structured_file(self):
        return self._structured_file

    @property
    def current_event(self) -> int | None:
        return self._current_event

    @property
    def driver_name(self) -> str:
        """Get the API/driver name of the current capture."""
        return self._cap.DriverName() if self._cap else "unknown"

    @property
    def action_map(self) -> dict[int, object]:
        """Get the event_id -> ActionDescription mapping."""
        return self._action_map

    def require_open(self) -> dict | None:
        """Return an error dict if no capture is open, else None."""
        if not self.is_open:
            return make_error("No capture file is open. Use open_capture first.", "NO_CAPTURE_OPEN")
        return None

    def open(self, filepath: str, remote_host: str | None = None) -> dict:
        """Open a .rdc capture file. Closes any previously open capture.

        If remote_host is given (e.g. "1.2.3.4" or "1.2.3.4:39920" for a
        RenderDoc remote server reachable via adb port forward on
        127.0.0.1:39920), the capture is copied to and replayed on that
        remote host's GPU instead of locally. Use this when the capture
        requires hardware features the local GPU lacks (e.g. ETC2 texture
        compression on a capture made on a mobile device).
        """
        self._ensure_initialized()

        if not os.path.isfile(filepath):
            return make_error(f"File not found: {filepath}", "API_ERROR")

        # Close any existing capture
        if self.is_open:
            self.close()

        cap = rd.OpenCaptureFile()
        result = cap.OpenFile(filepath, "", None)
        if result != rd.ResultCode.Succeeded:
            cap.Shutdown()
            return make_error(f"Failed to open file: {result}", "API_ERROR")

        remote = None
        if remote_host:
            remote_status, remote = rd.CreateRemoteServerConnection(remote_host)
            if remote_status != rd.ResultCode.Succeeded:
                cap.Shutdown()
                return make_error(
                    f"Failed to connect to remote server at {remote_host}: {remote_status}",
                    "REMOTE_CONNECT_ERROR",
                )

            remote_path = remote.CopyCaptureToRemote(filepath, None)
            if not remote_path:
                remote.ShutdownConnection()
                cap.Shutdown()
                return make_error(
                    f"Failed to copy capture to remote host {remote_host}", "REMOTE_COPY_ERROR"
                )

            # NoPreference asks the remote to replay with its own native
            # driver rather than proxying through a local one - what we want
            # when the remote device's GPU matches the capture.
            result, controller = remote.OpenCapture(
                rd.RemoteServer.NoPreference, remote_path, rd.ReplayOptions(), None
            )
            if result != rd.ResultCode.Succeeded:
                remote.ShutdownConnection()
                cap.Shutdown()
                return make_error(
                    f"Failed to initialize remote replay on {remote_host}: {result}", "API_ERROR"
                )
        else:
            if not cap.LocalReplaySupport():
                cap.Shutdown()
                return make_error("Capture cannot be replayed on this machine", "API_ERROR")

            result, controller = cap.OpenCapture(rd.ReplayOptions(), None)
            if result != rd.ResultCode.Succeeded:
                cap.Shutdown()
                return make_error(f"Failed to initialize replay: {result}", "API_ERROR")

        self._cap = cap
        self._controller = controller
        self._remote = remote
        self._remote_host = remote_host
        self._filepath = filepath
        self._current_event = None
        self._structured_file = controller.GetStructuredFile()

        # Build action map
        self._action_map = {}
        self._build_action_map(controller.GetRootActions())

        # Build resource caches
        self._resource_id_cache = {}
        self._texture_desc_cache = {}
        textures = controller.GetTextures()
        buffers = controller.GetBuffers()
        for tex in textures:
            key = str(tex.resourceId)
            self._resource_id_cache[key] = tex.resourceId
            self._texture_desc_cache[key] = tex
        for buf in buffers:
            key = str(buf.resourceId)
            self._resource_id_cache[key] = buf.resourceId
        for res in controller.GetResources():
            key = str(res.resourceId)
            if key not in self._resource_id_cache:
                self._resource_id_cache[key] = res.resourceId

        # Gather summary
        root_actions = controller.GetRootActions()

        return {
            "filepath": filepath,
            "api": cap.DriverName(),
            "remote_host": remote_host,
            "total_actions": len(self._action_map),
            "root_actions": len(root_actions),
            "textures": len(textures),
            "buffers": len(buffers),
        }

    def _build_action_map(self, actions):
        """Recursively index all actions by event_id."""
        for a in actions:
            self._action_map[a.eventId] = a
            if len(a.children) > 0:
                self._build_action_map(a.children)

    def resolve_resource_id(self, resource_id_str: str):
        """Resolve a resource ID string to a ResourceId object, or None."""
        return self._resource_id_cache.get(resource_id_str)

    def get_texture_desc(self, resource_id_str: str):
        """Get a TextureDescription by resource ID string, or None."""
        return self._texture_desc_cache.get(resource_id_str)

    def close(self) -> dict:
        """Close the current capture."""
        if not self.is_open:
            return {"status": "no capture was open"}

        filepath = self._filepath
        if self._remote is not None:
            # Controller is owned by the remote server connection; closing it
            # tears down replay on the remote side, then disconnect (leaves
            # the remote server process itself running on the device).
            self._remote.CloseCapture(self._controller)
            self._remote.ShutdownConnection()
        else:
            self._controller.Shutdown()
        self._cap.Shutdown()
        self._controller = None
        self._cap = None
        self._remote = None
        self._remote_host = None
        self._filepath = None
        self._current_event = None
        self._action_map = {}
        self._structured_file = None
        self._resource_id_cache = {}
        self._texture_desc_cache = {}
        return {"status": "closed", "filepath": filepath}

    def set_event(self, event_id: int) -> dict | None:
        """Navigate to a specific event. Returns error dict or None on success."""
        if event_id not in self._action_map:
            return make_error(f"Event ID {event_id} not found", "INVALID_EVENT_ID")
        self._controller.SetFrameEvent(event_id, True)
        self._current_event = event_id
        return None

    def ensure_event(self, event_id: int | None) -> dict | None:
        """If event_id is given, set it. If no event is current, return error.
        Returns error dict or None on success."""
        if event_id is not None:
            return self.set_event(event_id)
        if self._current_event is None:
            return make_error("No event selected. Use set_event or pass event_id.", "INVALID_EVENT_ID")
        return None

    def get_action(self, event_id: int):
        """Get an ActionDescription by event_id, or None."""
        return self._action_map.get(event_id)

    def get_root_actions(self):
        return self._controller.GetRootActions()

    def shutdown(self):
        """Full shutdown - close capture and deinitialize replay."""
        if self.is_open:
            self.close()
        if self._initialized:
            rd.ShutdownReplay()
            self._initialized = False


# Module-level singleton
_session: RenderDocSession | None = None


def get_session() -> RenderDocSession:
    """Get or create the global RenderDocSession singleton."""
    global _session
    if _session is None:
        _session = RenderDocSession()
    return _session
