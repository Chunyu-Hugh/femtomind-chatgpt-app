from tools.config import conf
from tools.reader import _READER

class READER(_READER):
    """LQCD moment tables: columns hadron, n, flav, value, stat_u, col.
    No kinematic cuts; uncertainties for n=2,3 (Alexandrou 2021) are
    pre-inflated by factor 2 in the data files per the paper."""
    def __init__(self):
        self.aux = conf['aux']

    def modify_table(self, tab):
        return tab
