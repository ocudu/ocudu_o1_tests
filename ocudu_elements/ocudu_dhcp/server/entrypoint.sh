#!/bin/sh

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

set -eu

. /etc/kea/dhcp_server.env
. generate_option43_hex.sh

cat > "etc/kea/subnets4.json" <<EOF
"subnet4": [
  {
    "subnet": "$SUBNET_IP_4",
    "id": 1,

    "pools": [
      {
        "pool": "$POOL_4"
      }
    ],

    "option-data": [
      {
        "name": "routers",
        "data": "$ROUTER_IP_4"
      },
      {
        "name": "domain-name-servers",
        "data": "$DNS_IP_4"
      },
      { 
        "name": "domain-name",
        "data": "$DOMAIN_NAME_4"
      },
      { 
        "name": "ntp-servers",
        "data": "$NTP_SERVERS_4"
      }
    ],

    "reservations": [
      {
        "hw-address": "$MAC_ADDRESS_RESERVATION_4",
        "ip-address": "$IP_ADDRESS_RESERVATION_4"
      }
    ]
  }
]
EOF

cat > "etc/kea/kea-dhcp4.conf" <<EOF
{
    "Dhcp4": {


        "interfaces-config": {
            "interfaces": ["$ETH_4"]
        },

        "lease-database": {
          "type": "memfile",
          "name": "kea-leases4.csv"
        },

        <?include "/etc/kea/subnets4.json"?>,

        "renew-timer": $RENEW_TIMER_4,
        "rebind-timer": $REBIND_TIMER_4,
        "valid-lifetime": $VALID_LIFETIME_4,

        "client-classes": [
        {
            "name": "ORAN-RU2",
            "test": "substring(option[60].text,0,10) == 'o-ran-ru2/'",
            "option-data": [
            {
                "code": 43,
                "space": "dhcp4",
                "csv-format": false,
                "always-send": true,
                "data": "$DHCP_OPTION_43_PAYLOAD_RU2"
            }
            ]
        },
        {
            "name": "ORAN-RU-LEGACY",
            "test": "substring(option[60].text,0,9) == 'o-ran-ru/'",
            "option-data": [
            {
                "code": 43,
                "space": "dhcp4",
                "csv-format": false,
                "always-send": true,
                "data": "$DHCP_OPTION_43_PAYLOAD_RU_LEGACY"
            }
            ]
        }
        ],

        "loggers": [
            {
                "name": "kea-dhcp4",
                "output_options": [
                    {
                        "output": "/var/log/kea/kea4.log"
                    }
                ],
                "severity": "INFO"
                }
        ]
    }
}
EOF

exec /usr/sbin/kea-dhcp4 -c /etc/kea/kea-dhcp4.conf
