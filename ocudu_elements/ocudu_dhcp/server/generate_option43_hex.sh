#!/bin/sh

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI
set -eu

# Generate DHCP Option 43 TLV payloads (RU2 and legacy).
# requires environment variables RU_CONTROLLER_IP_ADDRESS, RU_CONTROLLER_FQDN, CALLHOME_SSH_OR_TLS
# exports the payloads to be availabe for entrypoint.sh, which configures the DHCP server

# converts decimal number into hex
dec_to_hex() {
  printf '%02x' "$(($1))"
}

# builds IP data string as specified by O-RAN: tag + length(04) + IP address in hex 
tlv_ip() {
  t=$1
  ip=$2

  # temporarily change shell word splitting to split IP address at '.' into positional parameters
  old_ifs=$IFS
  IFS=.
  set -- $ip
  IFS=$old_ifs

  printf '%s04%s%s%s%s' \
    "$(dec_to_hex "$t")" \
    "$(dec_to_hex "$1")" "$(dec_to_hex "$2")" "$(dec_to_hex "$3")" "$(dec_to_hex "$4")"
}

# builds FQDN data string as specified by O-RAN: tag + length of ascii text + ASCII of FQDN in hex 
tlv_fqdn() {
  t=$1
  fqdn=$2
  len=${#fqdn}

  printf '%s%s' "$(dec_to_hex "$t")" "$(dec_to_hex "$len")"
  printf '%s' "$fqdn" | od -An -tx1 -v | tr -d ' \n'
}

# builds callhome data string as specified by O-RAN: tag + length(01) + callhome option
tlv_callhome_option() {
  t=$1
  v=$2
  printf '%s01%s' "$(dec_to_hex "$t")" "$(dec_to_hex "$v")"
}

# creates the TLV payload for the DHCP option 43 field, as a response to Vendor class identifier 'o-ran-ru2' (usual case)
DHCP_OPTION_43_PAYLOAD_RU2="$(
  printf '%s%s%s' \
    "$(tlv_ip 0x81 "$RU_CONTROLLER_IP_ADDRESS")" \
    "$(tlv_fqdn 0x82 "$RU_CONTROLLER_FQDN")" \
    "$(tlv_callhome_option 0x86 "$CALLHOME_SSH_OR_TLS")"
)"

# creates the TLV payload for the DHCP option 43 field, as a response to Vendor class identifier 'o-ran-ru' (legacy)
DHCP_OPTION_43_PAYLOAD_RU_LEGACY="$(
  printf '%s%s' \
    "$(tlv_ip 0x01 "$RU_CONTROLLER_IP_ADDRESS")" \
    "$(tlv_fqdn 0x02 "$RU_CONTROLLER_FQDN")"
)"

export DHCP_OPTION_43_PAYLOAD_RU2 DHCP_OPTION_43_PAYLOAD_RU_LEGACY