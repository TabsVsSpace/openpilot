#!/usr/bin/env python3
import struct
import traceback
from typing import Any

from tqdm import tqdm

import panda.python.uds as uds
from cereal import car
from selfdrive.car.fingerprints import FW_VERSIONS, get_attr_from_cars
from selfdrive.car.isotp_parallel_query import IsoTpParallelQuery
from selfdrive.car.toyota.values import CAR as TOYOTA
from selfdrive.swaglog import cloudlog

Ecu = car.CarParams.Ecu


def p16(val):
  return struct.pack("!H", val)


TESTER_PRESENT_REQUEST = bytes([uds.SERVICE_TYPE.TESTER_PRESENT, 0x0])
TESTER_PRESENT_RESPONSE = bytes([uds.SERVICE_TYPE.TESTER_PRESENT + 0x40, 0x0])

SHORT_TESTER_PRESENT_REQUEST = bytes([uds.SERVICE_TYPE.TESTER_PRESENT])
SHORT_TESTER_PRESENT_RESPONSE = bytes([uds.SERVICE_TYPE.TESTER_PRESENT + 0x40])

DEFAULT_DIAGNOSTIC_REQUEST = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL,
                                    uds.SESSION_TYPE.DEFAULT])
DEFAULT_DIAGNOSTIC_RESPONSE = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40,
                                    uds.SESSION_TYPE.DEFAULT, 0x0, 0x32, 0x1, 0xf4])

EXTENDED_DIAGNOSTIC_REQUEST = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL,
                                     uds.SESSION_TYPE.EXTENDED_DIAGNOSTIC])
EXTENDED_DIAGNOSTIC_RESPONSE = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40,
                                      uds.SESSION_TYPE.EXTENDED_DIAGNOSTIC, 0x0, 0x32, 0x1, 0xf4])

UDS_VERSION_REQUEST = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION)
UDS_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION)


HYUNDAI_VERSION_REQUEST_SHORT = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(0xf1a0)  # 4 Byte version number
HYUNDAI_VERSION_REQUEST_LONG = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(0xf100)  # Long description
HYUNDAI_VERSION_REQUEST_MULTI = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_SPARE_PART_NUMBER) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION) + \
  p16(0xf100) + \
  p16(0xf1a0)
HYUNDAI_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40])


TOYOTA_VERSION_REQUEST = b'\x1a\x88\x01'
TOYOTA_VERSION_RESPONSE = b'\x5a\x88\x01'

OBD_VERSION_REQUEST = b'\x09\x04'
OBD_VERSION_RESPONSE = b'\x49\x04'

SUBARU_VERSION_REQUEST = b'\x22\xf1\x82'
SUBARU_VERSION_RESPONSE = b'\x62\xf1\x82'


# supports subaddressing, request, response
REQUESTS = [
  # Hundai
  (
    "hyundai",
    [HYUNDAI_VERSION_REQUEST_SHORT],
    [HYUNDAI_VERSION_RESPONSE],
  ),
  (
    "hyundai",
    [HYUNDAI_VERSION_REQUEST_LONG],
    [HYUNDAI_VERSION_RESPONSE],
  ),
  (
    "hyundai",
    [HYUNDAI_VERSION_REQUEST_MULTI],
    [HYUNDAI_VERSION_RESPONSE],
  ),
  # Honda
  (
    "honda",
    [UDS_VERSION_REQUEST],
    [UDS_VERSION_RESPONSE],
  ),
  # Toyota
  (
    "toyota",
    [SHORT_TESTER_PRESENT_REQUEST, TOYOTA_VERSION_REQUEST],
    [SHORT_TESTER_PRESENT_RESPONSE, TOYOTA_VERSION_RESPONSE],
  ),
  (
    "toyota",
    [SHORT_TESTER_PRESENT_REQUEST, OBD_VERSION_REQUEST],
    [SHORT_TESTER_PRESENT_RESPONSE, OBD_VERSION_RESPONSE],
  ),
  (
    "toyota",
    [TESTER_PRESENT_REQUEST, DEFAULT_DIAGNOSTIC_REQUEST, EXTENDED_DIAGNOSTIC_REQUEST, UDS_VERSION_REQUEST],
    [TESTER_PRESENT_RESPONSE, DEFAULT_DIAGNOSTIC_RESPONSE, EXTENDED_DIAGNOSTIC_RESPONSE, UDS_VERSION_RESPONSE],
  ),
  # Subaru
  (
    "subaru",
    [TESTER_PRESENT_REQUEST, SUBARU_VERSION_REQUEST],
    [TESTER_PRESENT_RESPONSE, SUBARU_VERSION_RESPONSE],
  ),
]


def chunks(l, n=128):
  for i in range(0, len(l), n):
    yield l[i:i + n]


def match_fw_to_car(fw_versions):
  candidates = FW_VERSIONS
  invalid = []

  fw_versions_dict = {}
  for fw in fw_versions:
    addr = fw.address
    sub_addr = fw.subAddress if fw.subAddress != 0 else None
    fw_versions_dict[(addr, sub_addr)] = fw.fwVersion

  for candidate, fws in candidates.items():
    for ecu, expected_versions in fws.items():
      ecu_type = ecu[0]
      addr = ecu[1:]
      found_version = fw_versions_dict.get(addr, None)
      ESSENTIAL_ECUS = [Ecu.engine, Ecu.eps, Ecu.esp, Ecu.fwdRadar, Ecu.fwdCamera, Ecu.vsa, Ecu.electricBrakeBooster]
      if ecu_type == Ecu.esp and candidate in [TOYOTA.RAV4, TOYOTA.COROLLA, TOYOTA.HIGHLANDER] and found_version is None:
        continue

      # TODO: COROLLA_TSS2 engine can show on two different addresses
      if ecu_type == Ecu.engine and candidate in [TOYOTA.COROLLA_TSS2, TOYOTA.CHR] and found_version is None:
        continue

      # ignore non essential ecus
      if ecu_type not in ESSENTIAL_ECUS and found_version is None:
        continue

      if found_version not in expected_versions:
        invalid.append(candidate)
        break

  return set(candidates.keys()) - set(invalid)


def get_fw_versions(logcan, sendcan, bus, extra=None, timeout=0.1, debug=False, progress=False):
  ecu_types = {}

  # Extract ECU adresses to query from fingerprints
  # ECUs using a subadress need be queried one by one, the rest can be done in parallel
  addrs = []
  parallel_addrs = []

  versions = get_attr_from_cars('FW_VERSIONS', combine_brands=False)
  if extra is not None:
    versions.update(extra)

  for brand, brand_versions in versions.items():
    for c in brand_versions.values():
      for ecu_type, addr, sub_addr in c.keys():
        a = (brand, addr, sub_addr)
        if a not in ecu_types:
          ecu_types[(addr, sub_addr)] = ecu_type

        if sub_addr is None:
          if a not in parallel_addrs:
            parallel_addrs.append(a)
        else:
          if [a] not in addrs:
            addrs.append([a])

  addrs.insert(0, parallel_addrs)

  fw_versions = {}
  for i, addr in enumerate(tqdm(addrs, disable=not progress)):
    for addr_chunk in chunks(addr):
      for brand, request, response in REQUESTS:
        try:
          addrs = [(a, s) for (b, a, s) in addr_chunk if b in (brand, 'any')]

          if addrs:
            query = IsoTpParallelQuery(sendcan, logcan, bus, addrs, request, response, debug=debug)
            t = 2 * timeout if i == 0 else timeout
            fw_versions.update(query.get_data(t))
        except Exception:
          cloudlog.warning(f"FW query exception: {traceback.format_exc()}")

  # Build capnp list to put into CarParams
  car_fw = []
  for addr, version in fw_versions.items():
    f = car.CarParams.CarFw.new_message()

    f.ecu = ecu_types[addr]
    f.fwVersion = version
    f.address = addr[0]

    if addr[1] is not None:
      f.subAddress = addr[1]

    car_fw.append(f)

  return car_fw


if __name__ == "__main__":
  import time
  import argparse
  import cereal.messaging as messaging
  from selfdrive.car.vin import get_vin

  parser = argparse.ArgumentParser(description='Get firmware version of ECUs')
  parser.add_argument('--hex', action='store_true')
  parser.add_argument('--scan', action='store_true')
  parser.add_argument('--debug', action='store_true')
  args = parser.parse_args()

  logcan = messaging.sub_sock('can')
  sendcan = messaging.pub_sock('sendcan')

  extra: Any = None
  if args.scan:
    extra = {}
    # Honda
    for i in range(256):
      extra[(Ecu.unknown, 0x18da00f1 + (i << 8), None)] = []
      extra[(Ecu.unknown, 0x700 + i, None)] = []
      extra[(Ecu.unknown, 0x750, i)] = []
    extra = {"any": {"debug": extra}}

  time.sleep(10.)

  t = time.time()
  print("Getting vin...")
  addr, vin = get_vin(logcan, sendcan, 1, retry=10, debug=args.debug)
  print(f"VIN: {vin}")
  print("Getting VIN took %.3f s" % (time.time() - t))
  print()

  t = time.time()
  fw_vers = get_fw_versions(logcan, sendcan, 1, extra=extra, debug=args.debug, progress=True)
  candidates = match_fw_to_car(fw_vers)

  print()
  print("Found FW versions")
  print("{")
  for version in fw_vers:
    subaddr = None if version.subAddress == 0 else hex(version.subAddress)
    if args.hex:
      print(f"  (Ecu.{version.ecu}, {hex(version.address)}, {subaddr}): [b'%s']" % (''.join(r'\x{:02x}'.format(x) for x in version.fwVersion)))
    else:
      print(f"  (Ecu.{version.ecu}, {hex(version.address)}, {subaddr}): [{version.fwVersion}]")
  print("}")

  print()
  print("Possible matches:", candidates)
  print("Getting fw took %.3f s" % (time.time() - t))
