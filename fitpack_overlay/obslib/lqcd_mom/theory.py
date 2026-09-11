import numpy as np
from scipy.integrate import quad
from tools.config import conf

class MOMENTS:
    """Lattice PDF moments at mu = 2 GeV (Q2 = 4 GeV^2).

    Convention: momentum-fraction definition
        <x^n>_f = integral_0^1 dx x^n [ f(x,mu) + fbar(x,mu) ]
    for quark flavors and <x^n>_g = integral x^n g(x,mu).

    Note: Eq. (2) of arXiv:2510.11979 prints an alternating (-1)^{n+1}
    sign, but that convention cannot reproduce the positive sea-quark
    moments published in the paper's Table 1 (e.g. <x>_s^pi = 0.036(15)
    with s = sbar in our SU(3)-symmetric sea); the momentum-fraction
    definition matches all published values. Documented in L2_DESIGN.md.
    """
    def __init__(self):
        self.Q2 = 4.0  # mu = 2 GeV

    def _moment(self, pdf, flav, n):
        pdf.evolve(self.Q2)
        Q2 = self.Q2
        f = pdf.get_xF  # returns x*f(x)
        if n == 1:
            return quad(lambda x: f(x, Q2, flav, evolve=False), 0, 1, limit=200)[0]
        return quad(lambda x: x**(n-1) * f(x, Q2, flav, evolve=False), 0, 1, limit=200)[0]

    def get_theory(self, hadron, flav, n):
        pdf = conf['pdf-pion'] if hadron == 'pion' else conf['pdf-kaon']
        pairs = {'u': ('u','ub'), 'd': ('d','db'), 's': ('s','sb'),
                 'c': ('c','cb'), 'g': ('g',None)}
        a, b = pairs[flav]
        val = self._moment(pdf, a, n)
        if b is not None:
            val += self._moment(pdf, b, n)
        return val
