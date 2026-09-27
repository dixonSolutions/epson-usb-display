"""Virtual monitor on GNOME/Mutter, streamed as raw BGR frames.

Mutter's ScreenCast.RecordVirtual creates a real monitor that shows up in
Settings > Displays (extend, mirror, arrange). We pull its PipeWire stream
through GStreamer and hand each frame to a callback.
"""

import dbus
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

SC_BUS = "org.gnome.Mutter.ScreenCast"


class VirtualDisplay:
    def __init__(self, width, height, on_frame, fps=10):
        Gst.init(None)
        self.width, self.height, self.fps = width, height, fps
        self.on_frame = on_frame
        self.bus = dbus.SessionBus()
        self.pipeline = None
        self.session = None

    def start(self):
        sc = dbus.Interface(self.bus.get_object(SC_BUS, "/org/gnome/Mutter/ScreenCast"), SC_BUS)
        path = sc.CreateSession(dbus.Dictionary({}, signature="sv"))
        self.session = dbus.Interface(self.bus.get_object(SC_BUS, path), SC_BUS + ".Session")
        stream = self.session.RecordVirtual(dbus.Dictionary({
            "cursor-mode": dbus.UInt32(1),       # draw the cursor into frames
            "is-platform": dbus.Boolean(True),   # behave like a real output
        }, signature="sv"))
        self.bus.add_signal_receiver(self._on_stream, "PipeWireStreamAdded",
                                     SC_BUS + ".Stream", path=stream)
        self.session.Start()

    def _on_stream(self, node_id):
        # The virtual monitor takes its mode from the size we negotiate, so
        # request the projector's native resolution.
        desc = (
            f"pipewiresrc path={node_id} always-copy=true ! "
            f"video/x-raw,width={self.width},height={self.height},max-framerate={self.fps}/1 ! "
            f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
            f"videoconvert ! videoscale ! video/x-raw,format=BGR,width={self.width},height={self.height} ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(desc)
        self.pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                self.on_frame(bytes(info.data))
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.session:
            try:
                self.session.Stop()
            except dbus.DBusException:
                pass
            self.session = None
