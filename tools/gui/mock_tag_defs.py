"""Static tag definitions per PLC type for the offshore wind farm mock.

Each dict maps ``struct_name -> [(var_name, VariantType, initial_value), ...]``.
Used by :mod:`tools.gui.mock_opcua_server` to build the OPC UA address space.
"""

from __future__ import annotations

from typing import Any

from asyncua import ua

_BOOL = ua.VariantType.Boolean
_FLOAT = ua.VariantType.Float
_INT = ua.VariantType.Int32
_INT16 = ua.VariantType.Int16
_STR = ua.VariantType.String


# ---------------------------------------------------------------------------
# WTGxx -- Wind Turbine Generator
# ---------------------------------------------------------------------------

WTG_TAGS: dict[str, list[tuple[str, ua.VariantType, Any]]] = {
    "Turbine": [
        ("xRunning_Sts", _BOOL, True),
        ("xAvailable_Sts", _BOOL, True),
        ("xFault_Sts", _BOOL, False),
        ("xRemoteCtrl_Sts", _BOOL, True),
        ("sOperatingMode", _STR, "Producing"),
        ("xStart_Cmd", _BOOL, False),
        ("xStop_Cmd", _BOOL, False),
        ("xResetFault_Cmd", _BOOL, False),
        ("xYawAuto_Cmd", _BOOL, True),
    ],
    "Power": [
        ("rActivePower_kW", _FLOAT, 2500.0),
        ("rReactivePower_kVAr", _FLOAT, 250.0),
        ("rPowerFactor", _FLOAT, 0.98),
        ("rGeneratorRPM", _FLOAT, 1500.0),
        ("rGridFreq_Hz", _FLOAT, 50.0),
        ("rGridVoltage_V", _FLOAT, 690.0),
        ("rEnergyToday_kWh", _FLOAT, 12000.0),
        ("rEnergyTotal_MWh", _FLOAT, 25430.0),
    ],
    "Rotor": [
        ("rRotorRPM", _FLOAT, 15.0),
        ("rPitchAngle_deg", _FLOAT, 0.0),
        ("rTorque_kNm", _FLOAT, 1600.0),
        ("rBladeLoad_A", _FLOAT, 250.0),
        ("rBladeLoad_B", _FLOAT, 250.0),
        ("rBladeLoad_C", _FLOAT, 250.0),
    ],
    "Yaw": [
        ("rYawPosition_deg", _FLOAT, 270.0),
        ("rYawError_deg", _FLOAT, 0.0),
        ("xYawing_Sts", _BOOL, False),
    ],
    "Gearbox": [
        ("rOilTemp_C", _FLOAT, 55.0),
        ("rOilPressure_bar", _FLOAT, 3.5),
        ("rBearingTemp_C", _FLOAT, 60.0),
        ("rVibration_mm_s", _FLOAT, 3.5),
    ],
    "Nacelle": [
        ("rNacelleTemp_C", _FLOAT, 28.0),
        ("rHumidity_Pct", _FLOAT, 55.0),
        ("xDoorOpen_Sts", _BOOL, False),
        ("xFireDetected_Sts", _BOOL, False),
    ],
    "Conditions": [
        ("rWindSpeed_mps", _FLOAT, 11.0),
        ("rWindDirection_deg", _FLOAT, 270.0),
        ("rAmbientTemp_C", _FLOAT, 10.0),
    ],
    "Alarms": [
        ("xOverspeed", _BOOL, False),
        ("xVibrationHigh", _BOOL, False),
        ("xGridLoss", _BOOL, False),
        ("xConverterFault", _BOOL, False),
        ("xGearboxTempHigh", _BOOL, False),
        ("xPitchFault", _BOOL, False),
        ("xYawFault", _BOOL, False),
        ("xEmergencyStop_Active", _BOOL, False),
    ],
}


# ---------------------------------------------------------------------------
# SUB01 -- Offshore Substation
# ---------------------------------------------------------------------------

SUB_TAGS: dict[str, list[tuple[str, ua.VariantType, Any]]] = {
    "Transformer": [
        ("rPrimaryVoltage_kV", _FLOAT, 33.0),
        ("rSecondaryVoltage_kV", _FLOAT, 132.0),
        ("rOilTemp_C", _FLOAT, 55.0),
        ("rWindingTemp_C", _FLOAT, 70.0),
        ("rLoadCurrent_A", _FLOAT, 150.0),
        ("rTapPosition", _INT16, 9),
        ("xCoolingFansOn_Sts", _BOOL, True),
        ("xOilLevelLow_Sts", _BOOL, False),
    ],
    "Switchgear": [
        ("xBusbarA_Closed", _BOOL, True),
        ("xBusbarB_Closed", _BOOL, True),
        ("xTieBreaker_Closed", _BOOL, False),
        ("xExportBreaker_Closed", _BOOL, True),
        ("xIncomerA_Closed", _BOOL, True),
        ("xIncomerB_Closed", _BOOL, True),
    ],
    "Export": [
        ("rExportPower_MW", _FLOAT, 10.0),
        ("rExportReactive_MVAr", _FLOAT, 1.0),
        ("rExportCurrent_A", _FLOAT, 44.0),
        ("rExportVoltage_kV", _FLOAT, 132.0),
        ("rGridFreq_Hz", _FLOAT, 50.0),
        ("rPowerFactor", _FLOAT, 0.98),
    ],
    "Protection": [
        ("xEarthFault_Active", _BOOL, False),
        ("xOvercurrent_Trip", _BOOL, False),
        ("xBuchholz_Alarm", _BOOL, False),
        ("xDifferential_Trip", _BOOL, False),
        ("xArcFlash_Detected", _BOOL, False),
    ],
    "Auxiliary": [
        ("rUPSVoltage_V", _FLOAT, 230.0),
        ("rBatteryCharge_Pct", _FLOAT, 95.0),
        ("xDieselGenRunning_Sts", _BOOL, False),
        ("rRoomTemp_C", _FLOAT, 22.0),
        ("xHVACOk_Sts", _BOOL, True),
    ],
    "Commands": [
        ("xExportBreaker_Open_Cmd", _BOOL, False),
        ("xExportBreaker_Close_Cmd", _BOOL, False),
        ("xResetProtection_Cmd", _BOOL, False),
        ("xTapUp_Cmd", _BOOL, False),
        ("xTapDown_Cmd", _BOOL, False),
    ],
    "Alarms": [
        ("xTransformerOilHigh", _BOOL, False),
        ("xTransformerWindingHigh", _BOOL, False),
        ("xAuxPowerLoss", _BOOL, False),
        ("xGridLoss", _BOOL, False),
        ("xIslandingDetected", _BOOL, False),
    ],
}


# ---------------------------------------------------------------------------
# MET01 -- Meteorological Mast
# ---------------------------------------------------------------------------

MET_TAGS: dict[str, list[tuple[str, ua.VariantType, Any]]] = {
    "Wind": [
        ("rWindSpeed_10m", _FLOAT, 9.9),
        ("rWindSpeed_60m", _FLOAT, 10.5),
        ("rWindSpeed_90m", _FLOAT, 11.0),
        ("rWindDir_10m_deg", _FLOAT, 270.0),
        ("rWindDir_90m_deg", _FLOAT, 270.0),
        ("rGustSpeed_mps", _FLOAT, 14.5),
        ("rTurbulenceIntensity", _FLOAT, 0.08),
    ],
    "Weather": [
        ("rAirTemp_C", _FLOAT, 10.0),
        ("rAirPressure_hPa", _FLOAT, 1013.0),
        ("rHumidity_Pct", _FLOAT, 70.0),
        ("rPrecipitation_mm_h", _FLOAT, 0.0),
        ("rVisibility_km", _FLOAT, 15.0),
    ],
    "Sea": [
        ("rWaveHeight_m", _FLOAT, 2.0),
        ("rWavePeriod_s", _FLOAT, 7.0),
        ("rWaveDirection_deg", _FLOAT, 265.0),
        ("rSeaTemp_C", _FLOAT, 12.0),
        ("rTideLevel_m", _FLOAT, 1.5),
        ("rCurrentSpeed_mps", _FLOAT, 0.4),
    ],
    "Derived": [
        ("xIcingRisk_Active", _BOOL, False),
        ("xExtremeWind_Active", _BOOL, False),
        ("xLowVisibility_Active", _BOOL, False),
        ("sWeatherCondition", _STR, "Cloudy"),
    ],
    "Status": [
        ("xDataValid_Sts", _BOOL, True),
        ("xSensorFault_Sts", _BOOL, False),
        ("xCommOk_Sts", _BOOL, True),
    ],
}


# ---------------------------------------------------------------------------
# SCADA -- Plant Master
# ---------------------------------------------------------------------------

SCADA_TAGS: dict[str, list[tuple[str, ua.VariantType, Any]]] = {
    "Farm": [
        ("rTotalPower_MW", _FLOAT, 10.0),
        ("rTotalReactive_MVAr", _FLOAT, 1.0),
        ("rCapacityFactor_Pct", _FLOAT, 50.0),
        ("rAvailability_Pct", _FLOAT, 100.0),
        ("nTurbinesRunning", _INT, 4),
        ("nTurbinesFaulted", _INT, 0),
        ("rEnergyToday_MWh", _FLOAT, 48.0),
        ("rEnergyMonth_GWh", _FLOAT, 1.4),
        ("rRevenue_EUR_today", _FLOAT, 3840.0),
    ],
    "Grid": [
        ("rGridPrice_EUR_MWh", _FLOAT, 80.0),
        ("rSetpointPower_MW", _FLOAT, 20.0),
        ("rCurtailment_Pct", _FLOAT, 0.0),
        ("xCurtailmentActive_Sts", _BOOL, False),
        ("xGridConnected_Sts", _BOOL, True),
    ],
    "Commands": [
        ("xFarmStart_Cmd", _BOOL, False),
        ("xFarmStop_Cmd", _BOOL, False),
        ("xEmergencyStopAll_Cmd", _BOOL, False),
        ("xCurtailmentOn_Cmd", _BOOL, False),
        ("xCurtailmentOff_Cmd", _BOOL, False),
        ("xResetAllAlarms_Cmd", _BOOL, False),
        ("rPowerSetpoint_MW", _FLOAT, 20.0),
    ],
    "Maintenance": [
        ("sTurbineInService", _STR, "None"),
        ("nActiveWorkOrders", _INT, 0),
        ("xVesselOnSite_Sts", _BOOL, False),
    ],
    "Alarms": [
        ("xFarmWideStop_Active", _BOOL, False),
        ("xAnyTurbineFault", _BOOL, False),
        ("xSubstationFault", _BOOL, False),
        ("xMetStationFault", _BOOL, False),
        ("xCommsDegraded", _BOOL, False),
        ("xSCADA_Primary_Active", _BOOL, True),
    ],
}
