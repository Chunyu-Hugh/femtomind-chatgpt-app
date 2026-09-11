import numpy as np
from tools.config import conf

class RESIDUALS:
    def get_residuals(self):
        tabs = conf['lqcd_mom tabs']
        thy, exp, unc = [], [], []
        for k in tabs:
            tab = tabs[k]
            for i in range(len(tab['value'])):
                thy.append(conf['lqcd_mom'].get_theory(tab['hadron'][i],
                                                       tab['flav'][i],
                                                       int(tab['n'][i])))
                exp.append(tab['value'][i])
                unc.append(tab['stat_u'][i])
        thy = np.array(thy); exp = np.array(exp); unc = np.array(unc)
        res = (thy - exp) / unc
        self.res = res
        self.dim = len(res)
        return res, exp, thy

    def gen_report(self, verb=0, level=0):
        return ['LQCD moments chi2/npts: %3.2f' % (np.sum(self.res**2) / max(len(self.res), 1))]
