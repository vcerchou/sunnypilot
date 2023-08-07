// CAN msgs we care about
#define MAZDA_LKAS          0x243
#define MAZDA_LKAS2         0x249
#define MAZDA_LKAS_HUD      0x440
#define MAZDA_CRZ_CTRL      0x21c
#define MAZDA_CRZ_BTNS      0x09d
#define TI_STEER_TORQUE     0x24A
#define MAZDA_STEER_TORQUE  0x240
#define MAZDA_ENGINE_DATA   0x202
#define MAZDA_PEDALS        0x165
#define MAZDA_CRZ_EVENTS    0x21F

// Radar

#define MAZDA_CRZ_INFO      0x21B
#define MAZDA_RADAR_361     0x361
#define MAZDA_RADAR_362     0x362
#define MAZDA_RADAR_363     0x363
#define MAZDA_RADAR_364     0x364
#define MAZDA_RADAR_365     0x365
#define MAZDA_RADAR_366     0x366
#define MAZDA_RADAR_499     0x499

// CAN bus numbers
#define MAZDA_MAIN 0
#define MAZDA_AUX  1
#define MAZDA_CAM  2

const SteeringLimits MAZDA_STEERING_LIMITS = {
  .max_steer = 800,
  .max_rate_up = 10,
  .max_rate_down = 25,
  .max_rt_delta = 300,
  .max_rt_interval = 250000,
  .driver_torque_factor = 1,
  .driver_torque_allowance = 15,
  .type = TorqueDriverLimited,
};

const CanMsg MAZDA_TX_MSGS[] = {{MAZDA_LKAS, 0, 8}, {MAZDA_CRZ_BTNS, 0, 8}, {MAZDA_LKAS2, 1, 8}, {MAZDA_LKAS_HUD, 0, 8},
                                {MAZDA_CRZ_CTRL, 0, 8}, {MAZDA_CRZ_INFO, 0, 8}, {MAZDA_RADAR_361, 0, 8}, {MAZDA_RADAR_362, 0, 8},
                                {MAZDA_RADAR_363, 0, 8}, {MAZDA_RADAR_364, 0, 8}, {MAZDA_RADAR_365, 0, 8}, {MAZDA_RADAR_366, 0, 8}, 
                                {MAZDA_RADAR_499, 0, 8}};

AddrCheckStruct mazda_addr_checks[] = {
  {.msg = {{MAZDA_CRZ_EVENTS,   0, 8, .expected_timestep = 20000U}, { 0 }, { 0 }}},
  {.msg = {{MAZDA_CRZ_BTNS,     0, 8, .expected_timestep = 100000U}, { 0 }, { 0 }}},
  {.msg = {{MAZDA_STEER_TORQUE, 0, 8, .expected_timestep = 12000U}, { 0 }, { 0 }}},
  {.msg = {{MAZDA_ENGINE_DATA,  0, 8, .expected_timestep = 10000U}, { 0 }, { 0 }}},
  {.msg = {{MAZDA_PEDALS,       0, 8, .expected_timestep = 20000U}, { 0 }, { 0 }}},
};
#define MAZDA_ADDR_CHECKS_LEN (sizeof(mazda_addr_checks) / sizeof(mazda_addr_checks[0]))
addr_checks mazda_rx_checks = {mazda_addr_checks, MAZDA_ADDR_CHECKS_LEN};

AddrCheckStruct mazda_ti_addr_checks[] = {
  {.msg = {{TI_STEER_TORQUE,    1, 8, .expected_timestep = 22000U}}},
  // TI_STEER_TORQUE expected_timestep should be the same as the tx rate of MAZDA_LKAS2
};
#define MAZDA_TI_ADDR_CHECKS_LEN (sizeof(mazda_ti_addr_checks) / sizeof(mazda_ti_addr_checks[0]))
addr_checks mazda_ti_rx_checks = {mazda_ti_addr_checks, MAZDA_TI_ADDR_CHECKS_LEN};

// track msgs coming from OP so that we know what CAM msgs to drop and what to forward
static int mazda_rx_hook(CANPacket_t *to_push) {
  bool valid = addr_safety_check(to_push, &mazda_rx_checks, NULL, NULL, NULL, NULL);
  
  if (((GET_ADDR(to_push) == TI_STEER_TORQUE)) &&
      ((GET_BYTE(to_push, 0) == GET_BYTE(to_push, 1)))) {
    torque_interceptor_detected = 1;
    valid &= addr_safety_check(to_push, &mazda_ti_rx_checks, NULL, NULL, NULL, NULL);
  }

  if (valid && (GET_BUS(to_push) == MAZDA_MAIN)) {
    int addr = GET_ADDR(to_push);

    if (addr == MAZDA_ENGINE_DATA) {
      // sample speed: scale by 0.01 to get kph
      int speed = (GET_BYTE(to_push, 2) << 8) | GET_BYTE(to_push, 3);
      vehicle_moving = speed > 10; // moving when speed > 0.1 kph
    }

    if (!torque_interceptor_detected) {
      if (addr == MAZDA_STEER_TORQUE) {
        int torque_driver_new = GET_BYTE(to_push, 0) - 127;
        update_sample(&torque_driver, torque_driver_new);
      }
    }

    if (addr == MAZDA_ENGINE_DATA) {
      gas_pressed = (GET_BYTE(to_push, 4) || (GET_BYTE(to_push, 5) & 0xF0U));
    }

    if (addr == MAZDA_PEDALS) {
      brake_pressed = (GET_BYTE(to_push, 0) & 0x10U);
    }

    if (addr == MAZDA_CRZ_EVENTS) {
      bool cruise_engaged = GET_BYTE(to_push, 2) & 0x1U;
      pcm_cruise_check(cruise_engaged);
    }

    generic_rx_checks((addr == MAZDA_LKAS));
  }
  
  if (valid && (GET_BUS(to_push) == MAZDA_AUX)) {
    int addr = GET_ADDR(to_push);
    if (addr == TI_STEER_TORQUE) {
      int torque_driver_new = GET_BYTE(to_push, 0) - 126;
      update_sample(&torque_driver, torque_driver_new);
    }
  }

  return valid;
}

static int mazda_tx_hook(CANPacket_t *to_send) {

  int tx = 1;
  int addr = GET_ADDR(to_send);
  int bus = GET_BUS(to_send);

  if (!msg_allowed(to_send, MAZDA_TX_MSGS, sizeof(MAZDA_TX_MSGS)/sizeof(MAZDA_TX_MSGS[0]))) {
    tx = 0;
  }

  // Check if msg is sent on the main BUS
  if (bus == MAZDA_MAIN) {

    // steer cmd checks
    if (addr == MAZDA_LKAS) {
      int desired_torque = (((GET_BYTE(to_send, 0) & 0x0FU) << 8) | GET_BYTE(to_send, 1)) - 2048U;

      if (steer_torque_cmd_checks(desired_torque, -1, MAZDA_STEERING_LIMITS)) {
        tx = 0;
      }
    }

    // cruise buttons check
    if (addr == MAZDA_CRZ_BTNS) {
      // allow resume spamming while controls allowed, but
      // only allow cancel while contrls not allowed
      bool cancel_cmd = (GET_BYTE(to_send, 0) == 0x1U);
      if (!controls_allowed && !cancel_cmd) {
        tx = 0;
      }
    }
  }

  return tx;
}

static int mazda_fwd_hook(int bus, int addr) {
  int bus_fwd = -1;
  bool block = false;
  if (bus == MAZDA_MAIN) {
    bus_fwd = MAZDA_CAM;
  } else if (bus == MAZDA_CAM) {
    block |= (addr == MAZDA_LKAS);
    block |= (addr == MAZDA_LKAS_HUD);
    block |= (addr == MAZDA_CRZ_INFO);
    block |= (addr == MAZDA_CRZ_CTRL);
    block |= (addr == MAZDA_RADAR_361);
    block |= (addr == MAZDA_RADAR_362);
    block |= (addr == MAZDA_RADAR_363);
    block |= (addr == MAZDA_RADAR_364);
    block |= (addr == MAZDA_RADAR_365);
    block |= (addr == MAZDA_RADAR_366);
    
    if (!block) {
      bus_fwd = MAZDA_MAIN;
    }
  } else {
    // don't fwd
  }

  return bus_fwd;
}

static const addr_checks* mazda_init(uint16_t param) {
  UNUSED(param);
  return &mazda_rx_checks;
}

const safety_hooks mazda_hooks = {
  .init = mazda_init,
  .rx = mazda_rx_hook,
  .tx = mazda_tx_hook,
  .tx_lin = nooutput_tx_lin_hook,
  .fwd = mazda_fwd_hook,
};
