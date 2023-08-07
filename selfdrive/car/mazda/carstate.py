from cereal import car
from common.conversions import Conversions as CV
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from selfdrive.car.interfaces import CarStateBase
from selfdrive.car.mazda.values import DBC, LKAS_LIMITS, GEN1, BUTTON_STATES, TI_STATE

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)

    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR"]["GEAR"]

    self.crz_btns_counter = 0
    self.acc_active_last = False
    self.low_speed_alert = False
    self.lkas_allowed_speed = False
    self.lkas_disabled = False

    self.lkas_enabled = False
    self.prev_lkas_enabled = False

    self.buttonStates = BUTTON_STATES.copy()
    self.buttonStatesPrev = BUTTON_STATES.copy()

    self.ti_ramp_down = False
    self.ti_version = 1
    self.ti_state = TI_STATE.RUN
    self.ti_violation = 0
    self.ti_error = 0
    self.ti_lkas_allowed = False

  def update(self, cp, cp_cam, cp_body):

    ret = car.CarState.new_message()

    self.prev_mads_enabled = self.mads_enabled
    self.prev_lkas_enabled = self.lkas_enabled
    self.buttonStatesPrev = self.buttonStates.copy()

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["FL"],
      cp.vl["WHEEL_SPEEDS"]["FR"],
      cp.vl["WHEEL_SPEEDS"]["RL"],
      cp.vl["WHEEL_SPEEDS"]["RR"],
    )
    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    # Match panda speed reading
    speed_kph = cp.vl["ENGINE_DATA"]["SPEED"]
    ret.standstill = speed_kph < .1

    self.lkas_enabled = not self.lkas_disabled

    can_gear = int(cp.vl["GEAR"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))

    ret.genericToggle = bool(cp.vl["BLINK_INFO"]["HIGH_BEAMS"])
    ret.leftBlindspot = cp.vl["BSM"]["LEFT_BS_STATUS"] != 0
    ret.rightBlindspot = cp.vl["BSM"]["RIGHT_BS_STATUS"] != 0
    ret.leftBlinker, ret.rightBlinker = ret.leftBlinkerOn, ret.rightBlinkerOn = self.update_blinker_from_lamp(40, cp.vl["BLINK_INFO"]["LEFT_BLINK"] == 1,
                                                                                                                  cp.vl["BLINK_INFO"]["RIGHT_BLINK"] == 1)

    if self.CP.enableTorqueInterceptor:
      ret.steeringTorque = cp_body.vl["TI_FEEDBACK"]["TI_TORQUE_SENSOR"]
      self.ti_version = cp_body.vl["TI_FEEDBACK"]["VERSION_NUMBER"]
      self.ti_state = cp_body.vl["TI_FEEDBACK"]["STATE"] # DISCOVER = 0, OFF = 1, DRIVER_OVER = 2, RUN=3
      self.ti_violation = cp_body.vl["TI_FEEDBACK"]["VIOL"] # 0 = no violation
      self.ti_error = cp_body.vl["TI_FEEDBACK"]["ERROR"] # 0 = no error
      if self.ti_version > 1:
        self.ti_ramp_down = (cp_body.vl["TI_FEEDBACK"]["RAMP_DOWN"] == 1)

      ret.steeringPressed = abs(ret.steeringTorque) > LKAS_LIMITS.TI_STEER_THRESHOLD
      self.ti_lkas_allowed = not self.ti_ramp_down and self.ti_state == TI_STATE.RUN
    else:
      ret.steeringTorque = cp.vl["STEER_TORQUE"]["STEER_TORQUE_SENSOR"]
      ret.steeringPressed = abs(ret.steeringTorque) > LKAS_LIMITS.STEER_THRESHOLD

    ret.steeringAngleDeg = cp.vl["STEER"]["STEER_ANGLE"]  

    ret.steeringTorqueEps = cp.vl["STEER_TORQUE"]["STEER_TORQUE_MOTOR"]
    ret.steeringRateDeg = cp.vl["STEER_RATE"]["STEER_ANGLE_RATE"]

    # TODO: this should be from 0 - 1.
    ret.brakePressed = cp.vl["PEDALS"]["BRAKE_ON"] == 1
    ret.brake = cp.vl["BRAKE"]["BRAKE_PRESSURE"]

    ret.seatbeltUnlatched = cp.vl["SEATBELT"]["DRIVER_SEATBELT"] == 0
    ret.doorOpen = any([cp.vl["DOORS"]["FL"], cp.vl["DOORS"]["FR"],
                        cp.vl["DOORS"]["BL"], cp.vl["DOORS"]["BR"]])

    # TODO: this should be from 0 - 1.
    ret.gas = cp.vl["ENGINE_DATA"]["PEDAL_GAS"]
    ret.gasPressed = ret.gas > 0

    # Either due to low speed or hands off
    lkas_blocked = cp.vl["STEER_RATE"]["LKAS_BLOCK"] == 1

    if self.CP.minSteerSpeed > 0:
      # LKAS is enabled at 52kph going up and disabled at 45kph going down
      # wait for LKAS_BLOCK signal to clear when going up since it lags behind the speed sometimes
      if speed_kph > LKAS_LIMITS.ENABLE_SPEED and not lkas_blocked:
        self.lkas_allowed_speed = True
      elif speed_kph < LKAS_LIMITS.DISABLE_SPEED:
        self.lkas_allowed_speed = False
    else:
      self.lkas_allowed_speed = True

    # TODO: the signal used for available seems to be the adaptive cruise signal, instead of the main on
    #       it should be used for carState.cruiseState.nonAdaptive instead
    ret.cruiseState.available = cp_cam.vl["CRZ_CTRL"]["CRZ_AVAILABLE"] == 1
    ret.cruiseState.enabled = cp.vl["CRZ_EVENTS"]["CRUISE_ACTIVE_CAR_MOVING"] == 1
    ret.cruiseState.standstill = cp.vl["PEDALS"]["STANDSTILL"] == 1
    ret.cruiseState.speed = cp.vl["CRZ_EVENTS"]["CRZ_SPEED"] * CV.KPH_TO_MS

    if ret.cruiseState.enabled:
      if not self.lkas_allowed_speed and self.acc_active_last:
        self.low_speed_alert = True
      else:
        self.low_speed_alert = False

    # Check if LKAS is disabled due to lack of driver torque when all other states indicate
    # it should be enabled (steer lockout). Don't warn until we actually get lkas active
    # and lose it again, i.e, after initial lkas activation
    ret.steerFaultTemporary = self.lkas_allowed_speed and lkas_blocked

    self.acc_active_last = ret.cruiseState.enabled

    self.buttonStates["accelCruise"] = bool(cp.vl["CRZ_BTNS"]["SET_P"])
    self.buttonStates["decelCruise"] = bool(cp.vl["CRZ_BTNS"]["SET_M"])
    self.buttonStates["cancel"] = bool(cp.vl["CRZ_BTNS"]["CAN_OFF"])
    self.buttonStates["resumeCruise"] = bool(cp.vl["CRZ_BTNS"]["RES"])

    self.crz_btns_counter = cp.vl["CRZ_BTNS"]["CTR"]

    # camera signals
    self.lkas_disabled = cp_cam.vl["CAM_LANEINFO"]["LANE_LINES"] == 0
    self.cam_lkas = cp_cam.vl["CAM_LKAS"]
    self.cam_laneinfo = cp_cam.vl["CAM_LANEINFO"]
    ret.steerFaultPermanent = cp_cam.vl["CAM_LKAS"]["ERR_BIT_1"] == 1

    self.cp_cam = cp_cam
    self.cp = cp

    return ret

  @staticmethod
  def get_can_parser(CP):
    signals = [
      # sig_name, sig_address
      ("LEFT_BLINK", "BLINK_INFO"),
      ("RIGHT_BLINK", "BLINK_INFO"),
      ("HIGH_BEAMS", "BLINK_INFO"),
      ("STEER_ANGLE", "STEER"),
      ("STEER_ANGLE_RATE", "STEER_RATE"),
      ("STEER_TORQUE_SENSOR", "STEER_TORQUE"),
      ("STEER_TORQUE_MOTOR", "STEER_TORQUE"),
      ("FL", "WHEEL_SPEEDS"),
      ("FR", "WHEEL_SPEEDS"),
      ("RL", "WHEEL_SPEEDS"),
      ("RR", "WHEEL_SPEEDS"),
    ]

    checks = [
      # sig_address, frequency
      ("BLINK_INFO", 10),
      ("STEER", 67),
      ("STEER_RATE", 83),
      ("STEER_TORQUE", 83),
      ("WHEEL_SPEEDS", 100),
    ]

    if CP.carFingerprint in GEN1:
      signals += [
        ("LKAS_BLOCK", "STEER_RATE"),
        ("LKAS_TRACK_STATE", "STEER_RATE"),
        ("HANDS_OFF_5_SECONDS", "STEER_RATE"),
        ("CRUISE_ACTIVE_CAR_MOVING", "CRZ_EVENTS", 0),
        ("CRZ_SPEED", "CRZ_EVENTS"),
        ("STANDSTILL", "PEDALS"),
        ("BRAKE_ON", "PEDALS"),
        ("BRAKE_PRESSURE", "BRAKE"),
        ("GEAR", "GEAR"),
        ("DRIVER_SEATBELT", "SEATBELT"),
        ("FL", "DOORS"),
        ("FR", "DOORS"),
        ("BL", "DOORS"),
        ("BR", "DOORS"),
        ("PEDAL_GAS", "ENGINE_DATA"),
        ("SPEED", "ENGINE_DATA"),
        ("CTR", "CRZ_BTNS"),
        ("LEFT_BS_STATUS", "BSM"),
        ("RIGHT_BS_STATUS", "BSM"),

        ("SET_P", "CRZ_BTNS"),
        ("SET_M", "CRZ_BTNS"),
        ("CAN_OFF", "CRZ_BTNS"),
        ("RES", "CRZ_BTNS"),
      ]

      checks += [
        ("ENGINE_DATA", 100),
        ("CRZ_EVENTS", 50),
        ("CRZ_BTNS", 10),
        ("PEDALS", 50),
        ("BRAKE", 50),
        ("SEATBELT", 10),
        ("DOORS", 10),
        ("GEAR", 20),
        ("BSM", 10),
      ]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):
    signals = []
    checks = []

    if CP.carFingerprint in GEN1:
      signals += [
        # sig_name, sig_address
        ("LKAS_REQUEST", "CAM_LKAS"),
        ("CTR", "CAM_LKAS"),
        ("ERR_BIT_1", "CAM_LKAS"),
        ("LINE_NOT_VISIBLE", "CAM_LKAS"),
        ("BIT_1", "CAM_LKAS"),
        ("ERR_BIT_2", "CAM_LKAS"),
        ("STEERING_ANGLE", "CAM_LKAS"),
        ("ANGLE_ENABLED", "CAM_LKAS"),
        ("CHKSUM", "CAM_LKAS"),

        ("LINE_VISIBLE", "CAM_LANEINFO"),
        ("LINE_NOT_VISIBLE", "CAM_LANEINFO"),
        ("LANE_LINES", "CAM_LANEINFO"),
        ("BIT1", "CAM_LANEINFO"),
        ("BIT2", "CAM_LANEINFO"),
        ("BIT3", "CAM_LANEINFO"),
        ("NO_ERR_BIT", "CAM_LANEINFO"),
        ("S1", "CAM_LANEINFO"),
        ("S1_HBEAM", "CAM_LANEINFO"),
      ]

      checks += [
        # sig_address, frequency
        ("CAM_LANEINFO", 2),
        ("CAM_LKAS", 16),
      ]
      signals += [
        ("CRZ_ACTIVE", "CRZ_CTRL", 0),
        ("CRZ_AVAILABLE", "CRZ_CTRL", 0),
        ("DISTANCE_SETTING", "CRZ_CTRL", 0),
        ("ACC_ACTIVE_2", "CRZ_CTRL", 0),
        ("DISABLE_TIMER_1", "CRZ_CTRL", 0),
        ("DISABLE_TIMER_2", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_1", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_2", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_3", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_4", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_5", "CRZ_CTRL", 0),
        ("NEW_SIGNAL_6", "CRZ_CTRL", 0),
      ]
      signals += [
        ("STATUS", "CRZ_INFO", 0),
        ("STATIC_1", "CRZ_INFO", 0),
        ("ACCEL_CMD", "CRZ_INFO", 0),
        ("CRZ_ENDED", "CRZ_INFO", 0),
        ("ACC_SET_ALLOWED", "CRZ_INFO", 0),
        ("ACC_ACTIVE", "CRZ_INFO", 0),
        ("MYSTERY_BIT", "CRZ_INFO", 0),
        ("CTR1", "CRZ_INFO", 0),
        ("CHECKSUM", "CRZ_INFO", 0),
      ]

      checks += [("CRZ_CTRL",50), ("CRZ_INFO",50)]

      for addr in range(361,367):
        msg = f"RADAR_{addr}"
        signals += [
          ("MSGS_1", msg, 0),
          ("MSGS_2", msg, 0),
          ("CTR", msg, 0),
        ]
        checks += [(msg, 10)]
        
    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2)

  @staticmethod
  def get_body_can_parser(CP):
    # this function generates lists for signal, messages and initial values
    signals = []
    checks = []
    # get real driver torque if we are using a torque interceptor
    if CP.enableTorqueInterceptor:
      signals += [
        ("TI_TORQUE_SENSOR", "TI_FEEDBACK", 0),
        ("CHKSUM", "TI_FEEDBACK", 0),
        ("VERSION_NUMBER", "TI_FEEDBACK", 0),
        ("STATE", "TI_FEEDBACK", 0),
        ("VIOL", "TI_FEEDBACK", 0),
        ("ERROR", "TI_FEEDBACK", 0),
        ("RAMP_DOWN", "TI_FEEDBACK", 0),
      ]

      checks += [
        ("TI_FEEDBACK", 50),
      ]

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 1) # changed to back to 0 because my OBD2 port works