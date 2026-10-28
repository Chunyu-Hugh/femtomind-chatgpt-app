"""Configuration-only SIDIS/SIA datasets; not an FF execution capability.

The supplied reference is authoritative, including its commented SIA entries.
Missing filters and normalizations are intentional; never synthesize defaults.
"""
from __future__ import annotations

import copy
import math

CONFIGURATION_ONLY_REACTIONS = ('sidis', 'sia')
CONFIGURATION_REQUIRED_OBJECTS = {
    'sidis': ('pdf', 'ffpion', 'ffkaon', 'ffhadron'),
    'sia': ('ffpion', 'ffkaon', 'ffhadron'),
}
REFERENCE_SHA256 = '99cef1923bc4dab275defc7d1d6687ebbd6378aa2b22ded7798605afef2a9e12'

# SIDIS filters are filled from the card's configured Q2 cutoff.
CONFIGURATION_DATASETS = {'sia': {'filters': {1001: ['z < 0.9 and z > 0.15'],
                     1002: ['z < 0.9 and z > 0.15'],
                     1003: ['z < 0.9 and z > 0.15'],
                     1004: ['z < 0.9 and z > 0.15'],
                     1005: ['z < 0.9 and z > 0.15'],
                     1006: ['z < 0.9 and z > 0.15'],
                     1007: ['z < 0.9 and z > 0.15'],
                     1008: ['z < 0.9 and z > 0.15'],
                     1010: ['z < 0.9 and z > 0.15'],
                     1011: ['z < 0.9 and z > 0.15'],
                     1013: ['z < 0.9 and z > 0.15'],
                     1014: ['z < 0.9 and z > 0.02'],
                     1015: ['z < 0.9 and z > 0.02'],
                     1016: ['z < 0.9 and z > 0.02'],
                     1017: ['z < 0.9 and z > 0.02'],
                     1018: ['z < 0.9 and z > 0.02'],
                     1019: ['z < 0.9 and z > 0.02'],
                     1023: ['z < 0.9 and z > 0.02'],
                     1024: ['z < 0.9 and z > 0.02'],
                     1025: ['z < 0.9 and z > 0.02'],
                     1026: ['z < 0.9 and z > 0.02'],
                     1027: ['z < 0.9 and z > 0.02'],
                     1028: ['z < 0.9 and z > 0.15'],
                     1029: ['z < 0.9 and z > 0.15'],
                     1030: ['z < 0.9 and z > 0.15'],
                     2001: ['z < 0.9 and z > 0.15'],
                     2002: ['z < 0.9 and z > 0.15'],
                     2003: ['z < 0.9 and z > 0.15'],
                     2004: ['z < 0.9 and z > 0.15'],
                     2005: ['z < 0.9 and z > 0.15'],
                     2006: ['z < 0.9 and z > 0.15'],
                     2007: ['z < 0.9 and z > 0.15'],
                     2008: ['z < 0.9 and z > 0.15'],
                     2013: ['z < 0.9 and z > 0.15'],
                     2014: ['z < 0.9 and z > 0.02'],
                     2015: ['z < 0.9 and z > 0.02'],
                     2016: ['z < 0.9 and z > 0.02'],
                     2017: ['z < 0.9 and z > 0.02', 'value>0.0'],
                     2018: ['z < 0.9 and z > 0.02'],
                     2019: ['z < 0.9 and z > 0.02'],
                     2023: ['z < 0.9 and z > 0.02'],
                     2024: ['z < 0.9 and z > 0.02'],
                     2025: ['z < 0.9 and z > 0.02'],
                     2026: ['z < 0.9 and z > 0.02'],
                     2027: ['z < 0.9 and z > 0.02'],
                     2028: ['z < 0.9 and z > 0.2'],
                     2029: ['z < 0.9 and z > 0.15'],
                     2030: ['z < 0.9 and z > 0.15'],
                     2031: ['z < 0.9 and z > 0.02'],
                     4000: ['z < 0.9 and z > 0.02'],
                     4001: ['z < 0.9 and z > 0.02'],
                     4002: ['z < 0.9 and z > 0.02'],
                     4003: ['z < 0.9 and z > 0.15'],
                     4004: ['z < 0.9 and z > 0.15'],
                     4005: ['z < 0.9 and z > 0.02'],
                     4006: ['z < 0.9 and z > 0.02'],
                     4007: ['z < 0.9 and z > 0.02'],
                     4008: ['z < 0.9 and z > 0.15'],
                     4009: ['z < 0.9 and z > 0.15'],
                     4010: ['z < 0.9 and z > 0.15'],
                     4011: ['z < 0.9 and z > 0.15'],
                     4012: ['z < 0.9 and z > 0.15'],
                     4013: ['z < 0.9 and z > 0.02'],
                     4014: ['z < 0.9 and z > 0.02'],
                     4015: ['z < 0.9 and z > 0.02'],
                     4016: ['z < 0.9 and z > 0.02'],
                     4017: ['z < 0.9 and z > 0.02']},
         'norm': {1001: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 1.10478},
                  1002: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.982581},
                  1003: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 1.03054},
                  1004: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 1.03054},
                  1005: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 1.03419},
                  1006: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.979162},
                  1014: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 0.99777},
                  1015: {'fixed': 1014, 'max': 1.5, 'min': 0.5, 'value': 0.99777},
                  1016: {'fixed': 1014, 'max': 1.5, 'min': 0.5, 'value': 1.1892},
                  1017: {'fixed': 1014, 'max': 1.5, 'min': 0.5, 'value': 1.00345},
                  1018: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.02378},
                  1023: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.33434},
                  1024: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.19151},
                  1028: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 0.976001},
                  1029: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.868358},
                  1030: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.01509},
                  2001: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.983394},
                  2002: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.983394},
                  2003: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.994421},
                  2004: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.994421},
                  2005: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 0.992876},
                  2014: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 0.912186},
                  2015: {'fixed': 2014, 'max': 1.5, 'min': 0.5, 'value': 0.912186},
                  2016: {'fixed': 2014, 'max': 1.5, 'min': 0.5, 'value': 1.03996},
                  2017: {'fixed': 2014, 'max': 1.5, 'min': 0.5, 'value': 1.00015},
                  2018: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 0.888453},
                  2023: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.35922},
                  2024: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.38163},
                  2028: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 0.997435},
                  2029: {'fixed': False, 'max': 2.0, 'min': 0.1, 'value': 1.0},
                  2030: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.00482},
                  4000: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4002: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4003: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4008: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4009: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4010: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4011: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4012: {'fixed': False, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4014: {'fixed': 4002, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4015: {'fixed': 4002, 'max': 1.5, 'min': 0.5, 'value': 1.0},
                  4016: {'fixed': 4002, 'max': 1.5, 'min': 0.5, 'value': 1.0}},
         'xlsx': {1001: 'sia/expdata_V1/1001freenorm.xlsx',
                  1002: 'sia/expdata_V1/1002freenorm.xlsx',
                  1003: 'sia/expdata_V1/1003freenorm.xlsx',
                  1004: 'sia/expdata_V1/1004freenorm.xlsx',
                  1005: 'sia/expdata_V1/1005.xlsx',
                  1006: 'sia/expdata_V1/1006.xlsx',
                  1007: 'sia/expdata_V1/1007.xlsx',
                  1008: 'sia/expdata_V1/1008.xlsx',
                  1010: 'sia/expdata_V1/1010.xlsx',
                  1011: 'sia/expdata_V1/1011.xlsx',
                  1012: 'sia/expdata_V1/1012.xlsx',
                  1013: 'sia/expdata_V1/1013.xlsx',
                  1014: 'sia/expdata_V1/1014.xlsx',
                  1015: 'sia/expdata_V1/1015.xlsx',
                  1016: 'sia/expdata_V1/1016.xlsx',
                  1017: 'sia/expdata_V1/1017.xlsx',
                  1018: 'sia/expdata_V1/1018.xlsx',
                  1019: 'sia/expdata_V1/1019.xlsx',
                  1023: 'sia/expdata_V1/1023.xlsx',
                  1024: 'sia/expdata_V1/1024.xlsx',
                  1025: 'sia/expdata_V1/1025.xlsx',
                  1026: 'sia/expdata_V1/1026.xlsx',
                  1027: 'sia/expdata_V1/1027.xlsx',
                  1028: 'sia/expdata_V1/1028corrmat.xlsx',
                  1029: 'sia/expdata_V1/1029freenorm.xlsx',
                  1030: 'sia/expdata_V1/1030.xlsx',
                  2001: 'sia/expdata_V1/2001freenorm.xlsx',
                  2002: 'sia/expdata_V1/2002freenorm.xlsx',
                  2003: 'sia/expdata_V1/2003freenorm.xlsx',
                  2004: 'sia/expdata_V1/2004freenorm.xlsx',
                  2005: 'sia/expdata_V1/2005.xlsx',
                  2006: 'sia/expdata_V1/2006.xlsx',
                  2007: 'sia/expdata_V1/2007.xlsx',
                  2008: 'sia/expdata_V1/2008.xlsx',
                  2012: 'sia/expdata_V1/2012.xlsx',
                  2013: 'sia/expdata_V1/2013.xlsx',
                  2014: 'sia/expdata_V1/2014.xlsx',
                  2015: 'sia/expdata_V1/2015.xlsx',
                  2016: 'sia/expdata_V1/2016.xlsx',
                  2017: 'sia/expdata_V1/2017.xlsx',
                  2018: 'sia/expdata_V1/2018.xlsx',
                  2019: 'sia/expdata_V1/2019.xlsx',
                  2023: 'sia/expdata_V1/2023.xlsx',
                  2024: 'sia/expdata_V1/2024.xlsx',
                  2025: 'sia/expdata_V1/2025.xlsx',
                  2026: 'sia/expdata_V1/2026.xlsx',
                  2027: 'sia/expdata_V1/2027.xlsx',
                  2028: 'sia/expdata_V1/2028corrmat.xlsx',
                  2029: 'sia/expdata_V1/2029freenorm.xlsx',
                  2030: 'sia/expdata_V1/2030.xlsx',
                  2031: 'sia/expdata_V1/2031.xlsx',
                  4000: 'sia/expdata_V1/4000.xlsx',
                  4001: 'sia/expdata_V1/4001.xlsx',
                  4002: 'sia/expdata_V1/4002.xlsx',
                  4003: 'sia/expdata_V1/4003.xlsx',
                  4004: 'sia/expdata_V1/4004.xlsx',
                  4005: 'sia/expdata_V1/4005.xlsx',
                  4006: 'sia/expdata_V1/4006.xlsx',
                  4007: 'sia/expdata_V1/4007.xlsx',
                  4008: 'sia/expdata_V1/4008.xlsx',
                  4009: 'sia/expdata_V1/4009.xlsx',
                  4010: 'sia/expdata_V1/4010.xlsx',
                  4011: 'sia/expdata_V1/4011.xlsx',
                  4012: 'sia/expdata_V1/4012.xlsx',
                  4013: 'sia/expdata_V1/4013.xlsx',
                  4014: 'sia/expdata_V1/4014.xlsx',
                  4015: 'sia/expdata_V1/4015.xlsx',
                  4016: 'sia/expdata_V1/4016.xlsx',
                  4017: 'sia/expdata_V1/4017.xlsx'}},
 'sidis': {'norm': {},
           'xlsx': {1005: 'sidis/expdata/1005.xlsx',
                    1006: 'sidis/expdata/1006.xlsx',
                    2005: 'sidis/expdata/2005.xlsx',
                    2006: 'sidis/expdata/2006.xlsx',
                    3000: 'sidis/expdata/3000.xlsx',
                    3001: 'sidis/expdata/3001.xlsx',
                    1105: 'sidis/expdata/1105.xlsx',
                    1106: 'sidis/expdata/1106.xlsx',
                    1115: 'sidis/expdata/1115.xlsx',
                    1116: 'sidis/expdata/1116.xlsx',
                    2105: 'sidis/expdata/2105.xlsx',
                    2106: 'sidis/expdata/2106.xlsx',
                    2115: 'sidis/expdata/2115.xlsx',
                    2116: 'sidis/expdata/2116.xlsx',
                    3100: 'sidis/expdata/3100.xlsx',
                    3101: 'sidis/expdata/3101.xlsx',
                    3110: 'sidis/expdata/3110.xlsx',
                    3111: 'sidis/expdata/3111.xlsx'}}}

# Descriptions follow reference comments; asterisks remain UI-only.
CONFIGURATION_METADATA = {'sia': {1001: {'experiment': 'TASSO (12 GeV)', 'label': 'TASSO (12 GeV) — pion', 'not_normally_used': False},
         1002: {'experiment': 'TASSO (14 GeV)', 'label': 'TASSO (14 GeV) — pion', 'not_normally_used': False},
         1003: {'experiment': 'TASSO (22 GeV)', 'label': 'TASSO (22 GeV) — pion', 'not_normally_used': False},
         1004: {'experiment': 'TASSO (30 GeV)', 'label': 'TASSO (30 GeV) — pion', 'not_normally_used': False},
         1005: {'experiment': 'TASSO (34 GeV)', 'label': 'TASSO (34 GeV) — pion', 'not_normally_used': False},
         1006: {'experiment': 'TASSO (44 GeV)', 'label': 'TASSO (44 GeV) — pion', 'not_normally_used': False},
         1007: {'experiment': 'TPC 1984', 'label': 'TPC 1984 — pion', 'not_normally_used': True},
         1008: {'experiment': 'TPC 1988', 'label': 'TPC 1988 — pion', 'not_normally_used': True},
         1010: {'experiment': 'TPC(c)', 'label': 'TPC(c) — pion', 'not_normally_used': False},
         1011: {'experiment': 'TPC(b)', 'label': 'TPC(b) — pion', 'not_normally_used': False},
         1012: {'experiment': 'HRS', 'label': 'HRS — pion', 'not_normally_used': True},
         1013: {'experiment': 'TOPAZ', 'label': 'TOPAZ — pion', 'not_normally_used': False},
         1014: {'experiment': 'SLD', 'label': 'SLD — pion', 'not_normally_used': False},
         1015: {'experiment': 'SLD(uds)', 'label': 'SLD(uds) — pion', 'not_normally_used': False},
         1016: {'experiment': 'SLD(c)', 'label': 'SLD(c) — pion', 'not_normally_used': False},
         1017: {'experiment': 'SLD(b)', 'label': 'SLD(b) — pion', 'not_normally_used': False},
         1018: {'experiment': 'ALEPH', 'label': 'ALEPH — pion', 'not_normally_used': False},
         1019: {'experiment': 'OPAL', 'label': 'OPAL — pion', 'not_normally_used': False},
         1023: {'experiment': 'OPAL(c)', 'label': 'OPAL(c) — pion', 'not_normally_used': True},
         1024: {'experiment': 'OPAL(b)', 'label': 'OPAL(b) — pion', 'not_normally_used': True},
         1025: {'experiment': 'DELPHI', 'label': 'DELPHI — pion', 'not_normally_used': False},
         1026: {'experiment': 'DELPHI(uds)', 'label': 'DELPHI(uds) — pion', 'not_normally_used': False},
         1027: {'experiment': 'DELPHI(b)', 'label': 'DELPHI(b) — pion', 'not_normally_used': False},
         1028: {'experiment': 'BABAR', 'label': 'BABAR — pion', 'not_normally_used': False},
         1029: {'experiment': 'BELL', 'label': 'BELL — pion', 'not_normally_used': False},
         1030: {'experiment': 'ARGUS', 'label': 'ARGUS — pion', 'not_normally_used': False},
         2001: {'experiment': 'TASSO (12 GeV)', 'label': 'TASSO (12 GeV) — kaon', 'not_normally_used': False},
         2002: {'experiment': 'TASSO (14 GeV)', 'label': 'TASSO (14 GeV) — kaon', 'not_normally_used': False},
         2003: {'experiment': 'TASSO (22 GeV)', 'label': 'TASSO (22 GeV) — kaon', 'not_normally_used': False},
         2004: {'experiment': 'TASSO (30 GeV)', 'label': 'TASSO (30 GeV) — kaon', 'not_normally_used': False},
         2005: {'experiment': 'TASSO (34 GeV)', 'label': 'TASSO (34 GeV) — kaon', 'not_normally_used': False},
         2006: {'experiment': 'TASSO (44 Gev)', 'label': 'TASSO (44 Gev) — kaon', 'not_normally_used': False},
         2007: {'experiment': 'TPC 1984', 'label': 'TPC 1984 — kaon', 'not_normally_used': True},
         2008: {'experiment': 'TPC 1988', 'label': 'TPC 1988 — kaon', 'not_normally_used': False},
         2012: {'experiment': 'HRS', 'label': 'HRS — kaon', 'not_normally_used': True},
         2013: {'experiment': 'TOPAZ', 'label': 'TOPAZ — kaon', 'not_normally_used': False},
         2014: {'experiment': 'SLD', 'label': 'SLD — kaon', 'not_normally_used': False},
         2015: {'experiment': 'SLD(uds)', 'label': 'SLD(uds) — kaon', 'not_normally_used': False},
         2016: {'experiment': 'SLD(c)', 'label': 'SLD(c) — kaon', 'not_normally_used': False},
         2017: {'experiment': 'SLD(b)', 'label': 'SLD(b) — kaon', 'not_normally_used': False},
         2018: {'experiment': 'ALEPH', 'label': 'ALEPH — kaon', 'not_normally_used': False},
         2019: {'experiment': 'OPAL', 'label': 'OPAL — kaon', 'not_normally_used': False},
         2023: {'experiment': 'OPAL(c)', 'label': 'OPAL(c) — kaon', 'not_normally_used': True},
         2024: {'experiment': 'OPAL(b)', 'label': 'OPAL(b) — kaon', 'not_normally_used': True},
         2025: {'experiment': 'DELPHI', 'label': 'DELPHI — kaon', 'not_normally_used': False},
         2026: {'experiment': 'DELPHI(uds)', 'label': 'DELPHI(uds) — kaon', 'not_normally_used': False},
         2027: {'experiment': 'DELPHI(b)', 'label': 'DELPHI(b) — kaon', 'not_normally_used': False},
         2028: {'experiment': 'BABAR', 'label': 'BABAR — kaon', 'not_normally_used': False},
         2029: {'experiment': 'BELL', 'label': 'BELL — kaon', 'not_normally_used': False},
         2030: {'experiment': 'ARGUS', 'label': 'ARGUS — kaon', 'not_normally_used': False},
         2031: {'experiment': 'DELPHI 1995', 'label': 'DELPHI 1995 — kaon', 'not_normally_used': True},
         4000: {'experiment': 'ALEPH', 'label': 'ALEPH — unidentified hadron', 'not_normally_used': False},
         4001: {'experiment': 'DELPHI', 'label': 'DELPHI — unidentified hadron', 'not_normally_used': False},
         4002: {'experiment': 'SLD', 'label': 'SLD — unidentified hadron', 'not_normally_used': False},
         4003: {'experiment': 'TASSO (12 GeV)',
                'label': 'TASSO (12 GeV) — unidentified hadron',
                'not_normally_used': False},
         4004: {'experiment': 'TPC', 'label': 'TPC — unidentified hadron', 'not_normally_used': False},
         4005: {'experiment': 'OPAL(b)', 'label': 'OPAL(b) — unidentified hadron', 'not_normally_used': True},
         4006: {'experiment': 'OPAL(c)', 'label': 'OPAL(c) — unidentified hadron', 'not_normally_used': True},
         4007: {'experiment': 'OPAL', 'label': 'OPAL — unidentified hadron', 'not_normally_used': False},
         4008: {'experiment': 'TASSO (14 GeV)',
                'label': 'TASSO (14 GeV) — unidentified hadron',
                'not_normally_used': False},
         4009: {'experiment': 'TASSO (22 GeV)',
                'label': 'TASSO (22 GeV) — unidentified hadron',
                'not_normally_used': False},
         4010: {'experiment': 'TASSO (30 GeV)',
                'label': 'TASSO (30 GeV) — unidentified hadron',
                'not_normally_used': False},
         4011: {'experiment': 'TASSO (35 GeV)',
                'label': 'TASSO (35 GeV) — unidentified hadron',
                'not_normally_used': True},
         4012: {'experiment': 'TASSO (44 GeV)',
                'label': 'TASSO (44 GeV) — unidentified hadron',
                'not_normally_used': False},
         4013: {'experiment': 'DELPHI(b)',
                'label': 'DELPHI(b) — unidentified hadron',
                'not_normally_used': False},
         4014: {'experiment': 'SLD(c)', 'label': 'SLD(c) — unidentified hadron', 'not_normally_used': False},
         4015: {'experiment': 'SLD(b)', 'label': 'SLD(b) — unidentified hadron', 'not_normally_used': False},
         4016: {'experiment': 'SLD(uds)',
                'label': 'SLD(uds) — unidentified hadron',
                'not_normally_used': False},
         4017: {'experiment': 'DELPHI(uds)',
                'label': 'DELPHI(uds) — unidentified hadron',
                'not_normally_used': False}},
 'sidis': {1005: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , pi+ , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           1006: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , pi- , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           2005: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , K+  , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           2006: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , K-  , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           3000: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , h+  , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           3001: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , h-  , COMPASS (workbook of the published JAM24 fit)',
                  'not_normally_used': True},
           1105: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , pi+ , COMPASS',
                  'not_normally_used': False},
           1106: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , pi- , COMPASS',
                  'not_normally_used': False},
           1115: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , pi+ , COMPASS',
                  'not_normally_used': False},
           1116: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , pi- , COMPASS',
                  'not_normally_used': False},
           2105: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , K+  , COMPASS',
                  'not_normally_used': False},
           2106: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , K-  , COMPASS',
                  'not_normally_used': False},
           2115: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , K+  , COMPASS',
                  'not_normally_used': False},
           2116: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , K-  , COMPASS',
                  'not_normally_used': False},
           3100: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , h+  , COMPASS',
                  'not_normally_used': False},
           3101: {'experiment': 'COMPASS',
                  'label': 'deuteron , mult , h-  , COMPASS',
                  'not_normally_used': False},
           3110: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , h+  , COMPASS',
                  'not_normally_used': False},
           3111: {'experiment': 'COMPASS',
                  'label': 'proton   , mult , h-  , COMPASS',
                  'not_normally_used': False}}}


# These two included datasets have no filter definition in the supplied excerpt.
# configuration_dataset_block refuses them unless their cuts are given: FitPack's
# reader stops on a dataset that has no entry in a per-dataset filter mapping,
# and both keep no points after cuts anyway.
SIA_DATASETS_WITHOUT_FILTERS = (1012, 2012)


def sidis_dis_cut_values(cuts):
    """Resolve the two existing DIS cut controls, never W2cut_SIDIS."""
    values = []
    for variable in ('Q2', 'W2'):
        matches = [row['value'] for row in cuts if row.get('enabled', True)
                   and row.get('variable') == variable and row.get('op', '>') == '>']
        if len(matches) != 1 or isinstance(matches[0], bool):
            raise ValueError(f'SIDIS requires exactly one enabled DIS {variable} > cutoff')
        number = float(matches[0])
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f'SIDIS DIS {variable} cutoff must be positive and finite')
        values.append(number)
    return tuple(values)


# The COMPASS SIDIS workbooks the published JAM24 fit read (JAM24.final); the
# updated campaign defaults (JAM24.current) use 1105-3101 instead.
PUBLISHED_SIDIS_WORKBOOKS = frozenset({1005, 1006, 2005, 2006, 3000, 3001})


def configuration_dataset_block(reaction: str, dataset_ids, q2cut=None, w2cut=10.0,
                                normalizations=None, filters=None) -> dict:
    """Select exact dataset configuration without enabling FF execution.

    A linked normalization requires its reference dataset to be selected too.
    Never add that dataset implicitly or silently untie the normalization.
    """
    if reaction not in CONFIGURATION_ONLY_REACTIONS:
        raise ValueError(f'{reaction}: not a configuration-only reaction')
    selected = set()
    for dataset_id in dataset_ids:
        if isinstance(dataset_id, bool) or not str(dataset_id).isdigit():
            raise ValueError(f'{reaction}: invalid dataset ID {dataset_id!r}')
        selected.add(int(dataset_id))
    canonical = CONFIGURATION_DATASETS[reaction]
    unknown = selected - canonical['xlsx'].keys()
    if unknown:
        raise ValueError(f'{reaction}: unsupported datasets {sorted(unknown)}')
    if reaction == 'sia':
        given = {int(key) for key in filters if str(key).isdigit()} if isinstance(filters, dict) else set()
        unfiltered = sorted((selected & set(SIA_DATASETS_WITHOUT_FILTERS)) - given)
        if unfiltered:
            raise ValueError(f'sia: datasets {unfiltered} have no z cut in this configuration and keep no '
                             'points after cuts (FitPack stops on them); leave them out')
    block = copy.deepcopy(canonical)
    for key in ('xlsx', 'norm', 'filters'):
        if isinstance(block.get(key), dict):
            block[key] = {idx: entry for idx, entry in block[key].items() if idx in selected}
    if reaction == 'sidis':
        if isinstance(q2cut, bool):
            raise ValueError('sidis: the configured Q2 cutoff must be a positive finite number')
        try:
            cutoff = float(q2cut)
        except (TypeError, ValueError):
            raise ValueError('sidis: provide the configured Q2 cutoff') from None
        if not math.isfinite(cutoff) or cutoff <= 0:
            raise ValueError('sidis: the configured Q2 cutoff must be a positive finite number')
        if isinstance(w2cut, bool) or not math.isfinite(float(w2cut)) or float(w2cut) <= 0:
            raise ValueError('sidis: provide a positive finite DIS W2 cutoff')
        # FitPack derives W2/Z, then applies these reader filters once. Its
        # SIDIS reader does not inherit the IDIS filter list automatically.
        # The filters belong to the workbook generation: the published JAM24
        # fit read 1005-3001 with W2SIDIS>0 and 0.2<Z<0.8 (its input.py), the
        # updated workbooks 1105-3111 are read with 0.2<=Z<=0.8.
        published = selected & PUBLISHED_SIDIS_WORKBOOKS
        if published and published != selected:
            raise ValueError('sidis: do not mix the published JAM24 workbooks %s with the updated ones %s'
                             % (sorted(published), sorted(selected - published)))
        if published:
            block['filters'] = ['Q2>%f' % cutoff, 'W2>%f' % float(w2cut), 'W2SIDIS>0.000000', 'Z>0.2 and Z<0.8']
        else:
            block['filters'] = ['Q2>%f' % cutoff, 'W2>%f' % float(w2cut), 'Z>=0.2 and Z<=0.8']
    if filters is not None:
        if reaction != 'sia' or not isinstance(filters, dict):
            raise ValueError('Only SIA accepts explicit dataset filter mappings')
        replacement = {}
        for key, expressions in filters.items():
            if not str(key).isdigit() or int(key) not in selected or not isinstance(expressions, list):
                raise ValueError('SIA filters must address selected dataset IDs with expression lists')
            if any(not isinstance(expression, str) or not expression.strip() for expression in expressions):
                raise ValueError('SIA filter expressions must be nonempty strings')
            replacement[int(key)] = list(expressions)
        block['filters'] = replacement
    if normalizations is not None and not isinstance(normalizations, dict):
        raise ValueError('Normalization overrides must be a dataset-ID mapping')
    if normalizations:
        if reaction != 'sia' or not isinstance(normalizations, dict):
            raise ValueError('SIDIS has no normalization parameters')
        for key, definition in normalizations.items():
            if not str(key).isdigit() or int(key) not in block['norm'] or not isinstance(definition, dict):
                raise ValueError(f'{reaction}:{key}: no selected normalization parameter')
            entry = copy.deepcopy(block['norm'][int(key)])
            if set(definition) - {'value', 'min', 'max', 'fixed'}:
                raise ValueError('Normalization overrides accept value/min/max/fixed only; uncertainties stay in the workbook')
            entry.update(definition)
            fixed = entry['fixed']
            if isinstance(fixed, str) and fixed.isdigit():
                fixed = entry['fixed'] = int(fixed)
            if type(fixed) not in (bool, int):
                raise ValueError('Normalization fixed must be boolean or a linked dataset ID')
            if any(type(entry[f]) not in (int, float) or not math.isfinite(entry[f]) for f in ('value', 'min', 'max')):
                raise ValueError('Normalization value and bounds must be finite')
            if entry['min'] >= entry['max'] or fixed is False and not entry['min'] <= entry['value'] <= entry['max']:
                raise ValueError('Normalization needs ordered bounds containing a free starting value')
            if type(fixed) is int and (fixed not in selected or fixed not in block['norm'] or fixed == int(key)):
                raise ValueError('Normalization link must name another selected normalization parameter')
            block['norm'][int(key)] = entry
        for start in block['norm']:
            visited, current = set(), start
            while type(block['norm'][current]['fixed']) is int:
                if current in visited:
                    raise ValueError('Normalization links contain a cycle')
                visited.add(current);current = block['norm'][current]['fixed']
    for idx, norm in block['norm'].items():
        reference = norm.get('fixed')
        if type(reference) is int and reference not in selected:
            raise ValueError(
                f'{reaction}:{idx}: linked normalization requires selecting '
                f'{reaction}:{reference}; its reference cannot be dropped or untied')
    return block
