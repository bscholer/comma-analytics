"""Older openpilot logs (roughly 0.8.x to 0.9.7, and sunnypilot builds of that era) in today's shape.

openpilot has since moved or renamed the messages the dashboard reads. Their old copies are still in the logs, under
DEPRECATED names in the schema, so each segment is rewritten on read into the messages a current log would have:

- selfdriveState      from controlsState's old engagement / alert fields
- onroadEvents        from onroadEventsDEPRECATED (the old carEvents)
- driverMonitoringState from driverMonitoringStateDEPRECATED
- drivingModelData    from modelV2 (lane line probabilities, frame drops, the planned acceleration)
- gpsLocationExternal.hasFix from the old `flags` bit 0

A message is only made when the segment has none of its own, so current logs pass through untouched. Messages today's
schema can't read (sunnypilot's 2024 selfdriveStateSP, a different struct in the same slot) are dropped, and a log cut
off mid-write (power lost) keeps what was written before the cut.
"""

# old driver-monitoring event -> today's alert level
DM_EVENT_LEVELS = {
  "preDriverDistracted": "one", "preDriverUnresponsive": "one",
  "promptDriverDistracted": "two", "promptDriverUnresponsive": "two",
  "driverDistracted": "three", "driverUnresponsive": "three",
}


def _readable(events) -> list:
  msgs = []
  try:
    for m in events:
      if m.which() == "selfdriveStateSP":
        try:
          m.selfdriveStateSP.mads.active
        except Exception:
          continue
      msgs.append(m)
  except Exception:  # truncated: keep what came before
    pass
  return msgs


def upgrade(events, log) -> list:
  """`events` from one segment, with today's messages added for any that only exist in their old form."""
  msgs = _readable(events)
  have = {m.which() for m in msgs}
  if not have & {"controlsState", "onroadEventsDEPRECATED", "driverMonitoringStateDEPRECATED", "modelV2",
                  "gpsLocationExternal"}:
    return msgs
  make_selfdrive = "selfdriveState" not in have
  make_model = "drivingModelData" not in have
  out = []
  for m in msgs:
    w = m.which()
    if w == "gpsLocationExternal":
      g = m.gpsLocationExternal
      if not g.hasFix and g.flags & 1:
        b = m.as_builder()
        b.gpsLocationExternal.hasFix = True
        m = b.as_reader()
      out.append(m)
      continue
    out.append(m)
    if w == "controlsState" and make_selfdrive:
      out.append(_selfdrive(m, log))
    elif w == "onroadEventsDEPRECATED":
      out.append(_onroad_events(m, log))
    elif w == "driverMonitoringStateDEPRECATED":
      out.append(_driver_monitoring(m, log))
    elif w == "modelV2" and make_model:
      out.append(_driving_model(m, log))
  return out


def _event(m, log, which: str):
  e = log.Event.new_message()
  e.logMonoTime = m.logMonoTime
  e.valid = m.valid
  return e, e.init(which)


def _selfdrive(m, log):
  old = m.controlsState.deprecated
  e, s = _event(m, log, "selfdriveState")
  s.enabled = old.enabled
  s.active = old.active
  s.engageable = old.engageable
  s.state = old.state
  s.alertText1 = old.alertText1
  s.alertText2 = old.alertText2
  s.alertType = old.alertType
  s.alertStatus = old.alertStatus
  s.alertSize = old.alertSize
  s.experimentalMode = old.experimentalMode
  return e.as_reader()


def _set_names(dst_list, src) -> None:
  for d, s in zip(dst_list, src):
    d.name = _name(s)
    for f in ("enable", "noEntry", "warning", "userDisable", "softDisable", "immediateDisable", "preEnable",
              "permanent", "overrideLateral", "overrideLongitudinal"):
      setattr(d, f, getattr(s, f))


def _name(event) -> str | None:
  """An event's name; None for the few in neither enum (a fork's own events), which can't be named."""
  try:
    return str(event.name)
  except Exception:
    return None


def _names(events) -> list[str]:
  return [n for n in map(_name, events) if n]


def _known(src, names) -> list:
  """Old events whose name still exists today (a few were dropped); the rest can't be written into the new enum."""
  return [s for s in src if _name(s) in names]


def _onroad_events(m, log):
  names = set(log.OnroadEvent.EventName.schema.enumerants)
  src = _known(m.onroadEventsDEPRECATED, names)
  e = log.Event.new_message()
  e.logMonoTime = m.logMonoTime
  e.valid = m.valid
  _set_names(e.init("onroadEvents", len(src)), src)
  return e.as_reader()


def _driver_monitoring(m, log):
  old = m.driverMonitoringStateDEPRECATED
  names = _names(old.events) or _names(old.deprecated.events)
  level = max((DM_EVENT_LEVELS[n] for n in names if n in DM_EVENT_LEVELS), default="none",
              key=["none", "one", "two", "three"].index)
  e, d = _event(m, log, "driverMonitoringState")
  d.alertLevel = level
  # the old "active mode" was the camera judging attention; without it, wheel touches did
  d.activePolicy = "vision" if old.isActiveMode else "wheeltouch"
  d.isRHD = old.isRHD
  pct = max(-128, min(127, round(old.awarenessStatus * 100)))
  v = d.visionPolicyState
  v.awarenessPercent = pct
  v.isDistracted = old.isDistracted
  v.faceDetected = old.faceDetected
  d.wheeltouchPolicyState.awarenessPercent = pct
  return e.as_reader()


def _driving_model(m, log):
  old = m.modelV2
  e, d = _event(m, log, "drivingModelData")
  d.frameId = old.frameId
  d.frameDropPerc = old.frameDropPerc
  probs = list(old.laneLineProbs)
  if len(probs) >= 3:  # outer left, left, right, outer right
    d.laneLineMeta.leftProb = probs[1]
    d.laneLineMeta.rightProb = probs[2]
  d.action.desiredAcceleration = old.action.desiredAcceleration  # 0 before the model planned acceleration
  return e.as_reader()
