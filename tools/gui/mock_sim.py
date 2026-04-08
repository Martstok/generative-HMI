"""Offshore wind farm simulation engine for the mock OPC UA server.

Drives one shared ``world_state`` dict off a sine-based wind profile and pushes
physically-coherent values into the per-PLC OPC UA variables.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

from asyncua import ua
from loguru import logger

# Per-turbine offsets (WTG03 is the degraded unit for demo variety)
_WTG_OFFSETS = {
    "WTG01": 0.00,
    "WTG02": -0.02,
    "WTG03": -0.15,
    "WTG04": 0.01,
}


async def _write(var, value, vtype) -> None:
    """Write a value to an OPC UA variable, swallowing transient errors."""
    try:
        await var.write_value(value, vtype)
    except Exception as exc:
        logger.debug("write failed: {}", exc)


async def _wf(sim: dict, key: str, value: float) -> None:
    var = sim.get(key)
    if var is not None:
        await _write(var, float(value), ua.VariantType.Float)


async def _wb(sim: dict, key: str, value: bool) -> None:
    var = sim.get(key)
    if var is not None:
        await _write(var, bool(value), ua.VariantType.Boolean)


async def _wi(sim: dict, key: str, value: int) -> None:
    var = sim.get(key)
    if var is not None:
        await _write(var, int(value), ua.VariantType.Int32)


async def _ws(sim: dict, key: str, value: str) -> None:
    var = sim.get(key)
    if var is not None:
        await _write(var, str(value), ua.VariantType.String)


def _compute_wind(t: float) -> float:
    """Base wind speed at 90m, clamped 0..22 m/s."""
    base = 8.0 + 5.0 * math.sin(t * 0.05) + random.gauss(0.0, 0.6)
    return max(0.0, min(22.0, base))


async def _update_met(sim: dict, world: dict, t: float, dt: float) -> None:
    wind90 = _compute_wind(t)
    wind60 = wind90 * 0.95
    wind10 = wind90 * 0.90
    gust = wind90 * 1.3 + random.gauss(0.0, 0.8)
    wind_dir = 260.0 + 20.0 * math.sin(t * 0.01)
    turb = 0.05 + random.random() * 0.08

    world["wind_speed_90m"] = wind90
    world["wind_direction"] = wind_dir

    await _wf(sim, "Wind.rWindSpeed_10m", wind10)
    await _wf(sim, "Wind.rWindSpeed_60m", wind60)
    await _wf(sim, "Wind.rWindSpeed_90m", wind90)
    await _wf(sim, "Wind.rWindDir_10m_deg", wind_dir)
    await _wf(sim, "Wind.rWindDir_90m_deg", wind_dir)
    await _wf(sim, "Wind.rGustSpeed_mps", gust)
    await _wf(sim, "Wind.rTurbulenceIntensity", turb)

    air_temp = 10.0 + 5.0 * math.sin(t * 0.001) + random.gauss(0.0, 0.2)
    pressure = 1013.0 + 8.0 * math.sin(t * 0.0007)
    humidity = 65.0 + 15.0 * math.sin(t * 0.002)
    precip = max(0.0, random.gauss(0.0, 0.3)) if wind90 > 15 else 0.0
    visibility = max(1.0, 20.0 - wind90 * 0.5 - precip * 2.0)
    await _wf(sim, "Weather.rAirTemp_C", air_temp)
    await _wf(sim, "Weather.rAirPressure_hPa", pressure)
    await _wf(sim, "Weather.rHumidity_Pct", humidity)
    await _wf(sim, "Weather.rPrecipitation_mm_h", precip)
    await _wf(sim, "Weather.rVisibility_km", visibility)

    # Wave height tracks wind with ~5 min lag via EMA (alpha ~dt/300)
    alpha = dt / 300.0
    prev_wave = world.get("wave_height", 2.0)
    target = 0.5 + wind90 * 0.15
    wave_h = prev_wave + alpha * (target - prev_wave)
    world["wave_height"] = wave_h
    wave_period = 5.0 + wave_h * 1.2
    sea_temp = 12.0 + 1.5 * math.sin(t * 0.0005) + random.gauss(0.0, 0.05)
    tide = 1.5 + 1.2 * math.sin(t * 0.014)
    current = 0.3 + 0.2 * math.sin(t * 0.01)
    await _wf(sim, "Sea.rWaveHeight_m", wave_h)
    await _wf(sim, "Sea.rWavePeriod_s", wave_period)
    await _wf(sim, "Sea.rWaveDirection_deg", wind_dir - 5.0)
    await _wf(sim, "Sea.rSeaTemp_C", sea_temp)
    await _wf(sim, "Sea.rTideLevel_m", tide)
    await _wf(sim, "Sea.rCurrentSpeed_mps", current)

    await _wb(sim, "Derived.xIcingRisk_Active", air_temp < 2.0 and humidity > 80.0)
    await _wb(sim, "Derived.xExtremeWind_Active", wind90 > 20.0)
    await _wb(sim, "Derived.xLowVisibility_Active", visibility < 2.0)
    if wind90 > 20.0:
        cond = "Storm"
    elif precip > 0.5:
        cond = "Rain"
    elif visibility < 3.0:
        cond = "Fog"
    elif humidity > 75.0:
        cond = "Cloudy"
    else:
        cond = "Clear"
    await _ws(sim, "Derived.sWeatherCondition", cond)

    # Grid price sine 40..120 over 10 min cycle
    price = 80.0 + 40.0 * math.sin(t * (2 * math.pi / 600.0))
    world["grid_price"] = price


async def _update_wtg(
    plc_name: str, sim: dict, world: dict, t: float, dt: float, state: dict,
) -> None:
    wind = world.get("wind_speed_90m", 10.0)
    offset = _WTG_OFFSETS.get(plc_name, 0.0)

    # Fault injection for WTG02 every ~180s for 20s
    fault_active = False
    if plc_name == "WTG02":
        cycle = t % 180.0
        fault_active = cycle < 20.0 and t > 60.0
    await _wb(sim, "Alarms.xConverterFault", fault_active)

    below_cutin = wind < 3.0
    above_cutout = wind > 25.0
    running = not (below_cutin or above_cutout or fault_active)

    if running:
        raw_kw = min(5000.0, ((wind - 3.0) ** 3) * 4.0)
        power_kw = raw_kw * (1.0 + offset)
        power_kw = max(0.0, power_kw)
    else:
        power_kw = 0.0

    world.setdefault("wtg_powers", {})[plc_name] = power_kw

    reactive = power_kw * 0.1
    pf = 0.98 if running else 1.0
    gen_rpm = wind * 80.0 if running else 0.0
    rotor_rpm = gen_rpm / 100.0
    pitch = 0.0 if power_kw < 4500.0 else min(25.0, (power_kw - 4500.0) / 20.0)
    torque = (power_kw / rotor_rpm * 9.55) if rotor_rpm > 0.1 else 0.0

    # Energy accumulator (per-state dict)
    energy = state.get("energy_kwh", 12000.0) + power_kw * dt / 3600.0
    state["energy_kwh"] = energy

    await _wb(sim, "Turbine.xRunning_Sts", running)
    await _wb(sim, "Turbine.xAvailable_Sts", not fault_active)
    await _wb(sim, "Turbine.xFault_Sts", fault_active)
    await _wb(sim, "Turbine.xRemoteCtrl_Sts", True)
    if fault_active:
        mode = "Fault"
    elif below_cutin:
        mode = "Idle"
    elif above_cutout:
        mode = "Stopping"
    elif power_kw < 100.0:
        mode = "StartUp"
    else:
        mode = "Producing"
    await _ws(sim, "Turbine.sOperatingMode", mode)

    await _wf(sim, "Power.rActivePower_kW", power_kw)
    await _wf(sim, "Power.rReactivePower_kVAr", reactive)
    await _wf(sim, "Power.rPowerFactor", pf)
    await _wf(sim, "Power.rGeneratorRPM", gen_rpm)
    await _wf(sim, "Power.rGridFreq_Hz", 50.0 + random.gauss(0.0, 0.02))
    await _wf(sim, "Power.rGridVoltage_V", 690.0 + random.gauss(0.0, 2.0))
    await _wf(sim, "Power.rEnergyToday_kWh", energy)

    await _wf(sim, "Rotor.rRotorRPM", rotor_rpm)
    await _wf(sim, "Rotor.rPitchAngle_deg", pitch)
    await _wf(sim, "Rotor.rTorque_kNm", torque)
    load_base = power_kw / 10.0
    await _wf(sim, "Rotor.rBladeLoad_A", load_base + random.gauss(0.0, 5.0))
    await _wf(sim, "Rotor.rBladeLoad_B", load_base + random.gauss(0.0, 5.0))
    await _wf(sim, "Rotor.rBladeLoad_C", load_base + random.gauss(0.0, 5.0))

    yaw_pos = state.get("yaw_pos", 270.0)
    target_yaw = world.get("wind_direction", 270.0)
    yaw_pos += (target_yaw - yaw_pos) * 0.02
    state["yaw_pos"] = yaw_pos
    await _wf(sim, "Yaw.rYawPosition_deg", yaw_pos)
    await _wf(sim, "Yaw.rYawError_deg", target_yaw - yaw_pos)
    await _wb(sim, "Yaw.xYawing_Sts", abs(target_yaw - yaw_pos) > 2.0)

    gb_oil = 40.0 + power_kw / 100.0 + random.gauss(0.0, 0.5)
    bearing = 40.0 + power_kw / 100.0 + random.gauss(0.0, 0.5)
    vibration = 2.5 + power_kw / 2000.0 + random.gauss(0.0, 0.2)
    await _wf(sim, "Gearbox.rOilTemp_C", gb_oil)
    await _wf(sim, "Gearbox.rOilPressure_bar", 3.5 + random.gauss(0.0, 0.05))
    await _wf(sim, "Gearbox.rBearingTemp_C", bearing)
    await _wf(sim, "Gearbox.rVibration_mm_s", vibration)

    await _wf(sim, "Nacelle.rNacelleTemp_C", 25.0 + power_kw / 300.0)
    await _wf(sim, "Nacelle.rHumidity_Pct", 55.0 + random.gauss(0.0, 2.0))

    await _wf(sim, "Conditions.rWindSpeed_mps", wind + random.gauss(0.0, 0.3))
    await _wf(sim, "Conditions.rWindDirection_deg", target_yaw)
    await _wf(sim, "Conditions.rAmbientTemp_C", 10.0 + random.gauss(0.0, 0.2))

    await _wb(sim, "Alarms.xGearboxTempHigh", gb_oil > 75.0)
    await _wb(sim, "Alarms.xVibrationHigh", vibration > 6.0)


async def _update_sub(sim: dict, world: dict, t: float, dt: float) -> None:
    powers = world.get("wtg_powers", {})
    total_kw = sum(powers.values())
    export_mw = (total_kw / 1000.0) * 0.98
    world["export_mw"] = export_mw

    # 132kV export current, 3-phase: I = P / (sqrt(3) * V * pf)
    export_current = export_mw * 1000.0 / (132.0 * 1.732) if export_mw > 0 else 0.0
    load_current = export_mw * 1000.0 / (33.0 * 1.732) if export_mw > 0 else 0.0
    load_pct = min(1.0, export_mw / 20.0)
    oil_temp = 40.0 + load_pct * 30.0 + random.gauss(0.0, 0.3)
    winding_temp = oil_temp + 15.0 + load_pct * 20.0

    await _wf(sim, "Transformer.rPrimaryVoltage_kV", 33.0 + random.gauss(0.0, 0.1))
    await _wf(sim, "Transformer.rSecondaryVoltage_kV", 132.0 + random.gauss(0.0, 0.3))
    await _wf(sim, "Transformer.rOilTemp_C", oil_temp)
    await _wf(sim, "Transformer.rWindingTemp_C", winding_temp)
    await _wf(sim, "Transformer.rLoadCurrent_A", load_current)

    await _wf(sim, "Export.rExportPower_MW", export_mw)
    await _wf(sim, "Export.rExportReactive_MVAr", export_mw * 0.1)
    await _wf(sim, "Export.rExportCurrent_A", export_current)
    await _wf(sim, "Export.rExportVoltage_kV", 132.0 + random.gauss(0.0, 0.3))
    await _wf(sim, "Export.rGridFreq_Hz", 50.0 + random.gauss(0.0, 0.05))
    await _wf(sim, "Export.rPowerFactor", 0.98)

    await _wf(sim, "Auxiliary.rUPSVoltage_V", 230.0 + random.gauss(0.0, 1.0))
    await _wf(sim, "Auxiliary.rBatteryCharge_Pct", 95.0)
    await _wf(sim, "Auxiliary.rRoomTemp_C", 22.0 + random.gauss(0.0, 0.3))

    await _wb(sim, "Alarms.xTransformerOilHigh", oil_temp > 80.0)
    await _wb(sim, "Alarms.xTransformerWindingHigh", winding_temp > 100.0)


async def _update_scada(
    sim: dict, world: dict, t: float, dt: float, state: dict, wtg_states: dict,
) -> None:
    powers = world.get("wtg_powers", {})
    export_mw = world.get("export_mw", 0.0)
    running = sum(1 for _ in powers if powers[_] > 0.0)
    n_turbines = len(powers) or 4
    # Faulted = turbines with power==0 whose wind is in range (rough proxy)
    wind = world.get("wind_speed_90m", 10.0)
    if 3.0 <= wind <= 25.0:
        faulted = sum(1 for p in powers.values() if p == 0.0)
    else:
        faulted = 0
    availability = running / n_turbines * 100.0 if n_turbines else 0.0
    capacity_factor = export_mw / 20.0 * 100.0

    energy_mwh = state.get("energy_mwh", 48.0) + export_mw * dt / 3600.0
    state["energy_mwh"] = energy_mwh
    price = world.get("grid_price", 80.0)
    revenue = state.get("revenue", 3840.0) + export_mw * dt / 3600.0 * price
    state["revenue"] = revenue

    await _wf(sim, "Farm.rTotalPower_MW", export_mw)
    await _wf(sim, "Farm.rTotalReactive_MVAr", export_mw * 0.1)
    await _wf(sim, "Farm.rCapacityFactor_Pct", capacity_factor)
    await _wf(sim, "Farm.rAvailability_Pct", availability)
    await _wi(sim, "Farm.nTurbinesRunning", running)
    await _wi(sim, "Farm.nTurbinesFaulted", faulted)
    await _wf(sim, "Farm.rEnergyToday_MWh", energy_mwh)
    await _wf(sim, "Farm.rEnergyMonth_GWh", 1.4 + energy_mwh / 1000.0)
    await _wf(sim, "Farm.rRevenue_EUR_today", revenue)

    await _wf(sim, "Grid.rGridPrice_EUR_MWh", price)
    await _wb(sim, "Alarms.xAnyTurbineFault", faulted > 0)


async def run_simulation(
    world_state: dict,
    sim_vars: dict[str, dict[str, Any]],
    interval: float = 0.5,
) -> None:
    """Main simulation loop. Mutates ``world_state`` and writes to every PLC."""
    tick = 0
    wtg_internal: dict[str, dict] = {}
    sub_internal: dict = {}
    scada_internal: dict = {}
    world_state.setdefault("wtg_powers", {})

    while True:
        await asyncio.sleep(interval)
        tick += 1
        t = tick * interval

        # 1. Met mast drives the world
        if "MET01" in sim_vars:
            await _update_met(sim_vars["MET01"], world_state, t, interval)

        # 2. Each WTG (reads wind, writes power)
        for plc_name, sim in sim_vars.items():
            if not plc_name.startswith("WTG"):
                continue
            st = wtg_internal.setdefault(plc_name, {})
            await _update_wtg(plc_name, sim, world_state, t, interval, st)

        # 3. Substation (reads wtg powers)
        if "SUB01" in sim_vars:
            await _update_sub(sim_vars["SUB01"], world_state, t, interval)

        # 4. SCADA (reads everything)
        if "SCADA" in sim_vars:
            await _update_scada(
                sim_vars["SCADA"], world_state, t, interval,
                scada_internal, wtg_internal,
            )
