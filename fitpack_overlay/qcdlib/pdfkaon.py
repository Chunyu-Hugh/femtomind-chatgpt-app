#!/bin/env python
import sys
from scipy.special import gamma
import numpy as np
from qcdlib.alphaS import ALPHAS
from qcdlib.aux import AUX
from qcdlib.dglap import DGLAP
from qcdlib.kernels import KERNELS
from qcdlib.mellin import MELLIN
from tools.config import conf
from scipy.integrate import quad

class PDF:

    def __init__(self,mellin=None):

        self.spl='upol'
        self.Q20=conf['Q20']
        self.mc2=conf['aux'].mc2
        self.mb2=conf['aux'].mb2

        if mellin==None:
            self.kernel=KERNELS(conf['mellin-pion'],self.spl)
            self.dglap=DGLAP(conf['mellin-pion'],conf['alphaS'],self.kernel,conf['dglap mode'],conf['order'])
            self.mellin=conf['mellin-pion']
            self.pion = conf['pdf-pion']
        else:
            self.kernel=KERNELS(mellin,self.spl)
            self.dglap=DGLAP(mellin,conf['alphaS'],self.kernel,conf['dglap mode'],conf['order'])
            self.mellin=mellin
            if len(self.mellin.N) == 8: self.pion = conf['pdf-pion-mom']
            else: self.pion = conf['pdf-pion']
        #if 'mode' in conf: mode=conf['mode']
        #else: mode='truncated'
        self.set_params()
        self.setup()

    def set_params(self):
        """
        light valence: q_v(x) = norm* x**a * (1-x)**b * (1+c*x**d)
        others: f(x) = norm * x**a * (1-x)**b * (1+c*x**0.5+d*x)
        """
        params={}

        #--first shapes
        params['g1'] =np.array([1,-0.5,3,0,0])
        params['ubv1']=np.array([1, 0.5,3,0,0])
        params['sv1']=np.array([1, 0.5,3,0,0])
        params['u1']=np.array([1,-0.5,3,0,0])
        params['sb1']=np.array([1,-0.5,3,0,0])
        params['d1'] =np.array([0,-0.5,3,0,0])
        params['db1']=np.array([0,-0.5,3,0,0])
        #params['strange_sea_scaling']=np.array([1.0,0,0,0,0]) #--only parameter that matters here is the first; rest are irrelevant

        #--second shapes
        params['g2'] =np.array([0,-0.5,3,0,0])
        params['ubv2']=np.array([0, 0.5,3,0,0])
        params['sv2']=np.array([0, 0.5,3,0,0])
        params['u2']=np.array([0,-0.5,3,0,0])
        params['sb2']=np.array([0,-0.5,3,0,0])
        params['d2'] =np.array([0,-0.5,3,0,0])
        params['db2']=np.array([0,-0.5,3,0,0])
        #params['strange_sea_scaling2']=np.array([1.0,0,0,0,0]) #--only parameter that matters here is the first; rest are irrelevant

        #--third shapes
        #params['g3'] =np.array([0,-0.5,3,0,0])
        #params['ubv3']=np.array([0, 0.5,3,0,0])
        #params['dv3']=np.array([0, 0.5,3,0,0])
        #params['u3']=np.array([0,-0.5,3,0,0])
        #params['db3']=np.array([0,-0.5,3,0,0])
        #params['s3'] =np.array([0,-0.5,3,0,0])
        #params['sb3']=np.array([0,-0.5,3,0,0])

        self.params=params
        #self.FLAV=['g1','ubv1','sv1','u1','d1','db1','sb1','g2','ubv2','sv2','u2','d2','db2','sb2','strange_sea_scaling','strange_sea_scaling']
        self.FLAV=['g1','ubv1','sv1','u1','d1','db1','sb1','g2','ubv2','sv2','u2','d2','db2','sb2']
        self.PAR=['N','a','b','c','d']

    def set_sumrules(self):

        #--valence
        self.params['ubv1'][0]=1
        self.params['ubv1'][0]=(1-self.get_moments('ubv2',1))/self.get_moments('ubv1',1)

        #--because we fix the b parameter by the msr, we will do this step later!
        #self.params['sv1'][0]=1
        #self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)

        #--msr
        ubv = self.get_moments('ubv1',2) + self.get_moments('ubv2',2)

        #--because we fix the b parameter by the msr, we will do this step later!
        #sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)

        g  = self.get_moments('g1',2) + self.get_moments('g2',2)
        
        """
        Have to think more about this;
        We don't want the momentum sum rule influencing normalization of sea quarks 
        through the valence quark shapes.
        Instead, we equate all sea quark moments for kaon fitting scenarios 1-3.
        """
        #self.params['u1'][0]=6
        #self.params['u1'][0]=(1-ubv-sv-g-6*self.get_moments('u2',2))/self.get_moments('u1',2)

        #self.params['d1'][0]=self.params['u1'][0]
        #self.params['db1'][0]=self.params['u1'][0]
        #self.params['sb1'][0]=self.params['u1'][0]

        if conf['kaon fitting scenario'] == 1:
            u  = self.get_moments('u1',2) + self.get_moments('u2',2)
            d  = self.get_moments('d1',2) + self.get_moments('d2',2)
            db = self.get_moments('db1',2) + self.get_moments('db2',2)
            sb = self.get_moments('sb1',2) + self.get_moments('sb2',2)
    
            self.params['sv1'][0]=1
            self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)
            sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)


        
        elif conf['kaon fitting scenario'] == 2:
            """
            Here, we set the b parameter of the strange according to the momentum sum rule.
            i.e. b = ((a+1) - (a+2)*x_svK) / x_svK for a form such as N*x**a*(1-x)**b
            """

            u  = self.get_moments('u1',2) + self.get_moments('u2',2)
            d  = self.get_moments('d1',2) + self.get_moments('d2',2)
            db = self.get_moments('db1',2) + self.get_moments('db2',2)
            sb = self.get_moments('sb1',2) + self.get_moments('sb2',2)

            xsv = 1 - 2*u - ubv - d - db - 2*sb - g

            self.params['sv1'][2] = (self.params['sv1'][1] + 1 - (self.params['sv1'][1] + 2) * xsv) / xsv
            self.params['sv1'][0]=1
            self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)
            sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)

        elif conf['kaon fitting scenario'] == 3:
            """
            Here, we set the a parameter of the strange according to the momentum sum rule.
            i.e. a = ((b+2)*x_svK - 1) / (1 - x_svK) for a form such as N*x**a*(1-x)**b
            """
            
            u  = self.get_moments('u1',2) + self.get_moments('u2',2)
            d  = self.get_moments('d1',2) + self.get_moments('d2',2)
            db = self.get_moments('db1',2) + self.get_moments('db2',2)
            sb = self.get_moments('sb1',2) + self.get_moments('sb2',2)

            xsv = 1 - 2*u - ubv - d - db - 2*sb - g

            self.params['sv1'][1] = ((self.params['sv1'][2] + 2)*xsv - 1) / (1-xsv)
            self.params['sv1'][0]=1
            self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)
            sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)

        elif conf['kaon fitting scenario'] == 6:
            #--L2 design (paper text: sea/gluon shapes tied to pion, normalizations vary):
            #  sea N is a FREE parameter; gluon N fixed by the kaon momentum sum rule.
            #  valence Ns by baryon number. 9 free params total: ubv a,b,c,d + sv a,b,c,d + u1 N.
            self.params['sv1'][0]=1
            self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)

            ubv = self.get_moments('ubv1',2) + self.get_moments('ubv2',2)
            sv  = self.get_moments('sv1',2)  + self.get_moments('sv2',2)

            ss  = self.pion.params['strange_sea_scaling'][0]
            self.params['d1'][0] = self.params['u1'][0]
            self.params['db1'][0]= self.params['u1'][0]
            self.params['sb1'][0]= self.params['u1'][0] * ss
            sea2 = (self.get_moments('u1',2)  + self.get_moments('d1',2)
                  + self.get_moments('db1',2) + self.get_moments('sb1',2))

            g1m = self.get_moments('g1',2)
            if abs(g1m) > 1e-14:
                self.params['g1'][0] = self.params['g1'][0] * (1.0 - ubv - sv - sea2) / g1m
            sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)
            u  = self.get_moments('u1',2)  + self.get_moments('u2',2)
            d  = self.get_moments('d1',2)  + self.get_moments('d2',2)
            db = self.get_moments('db1',2) + self.get_moments('db2',2)
            sb = self.get_moments('sb1',2) + self.get_moments('sb2',2)
            g  = self.get_moments('g1',2)  + self.get_moments('g2',2)

        elif conf['kaon fitting scenario'] in [4,5]:
            """
            Here, we set the sea quark normalization according to the momentum sum rule.
            Also, here, we allow for the strange sea quark to have a scaling, but it is kept from the pion.
            """
            self.params['sv1'][0]=1
            self.params['sv1'][0]=(1-self.get_moments('sv2',1))/self.get_moments('sv1',1)

            ubv = self.get_moments('ubv1',2) + self.get_moments('ubv2',2)

            #xsv = 1 - 2*u - ubv - d - db - 2*sb - g

            # self.params['sv1'][1] = ((self.params['sv1'][2] + 2)*xsv - 1) / (1-xsv)
            sv = self.get_moments('sv1',2) + self.get_moments('sv2',2)

            strange_sea_scaling = self.pion.params['strange_sea_scaling'][0] #--this is a parameter that can be fitted that scales the strange quark in the sea down by some factor relative to the light sea quarks
            strange_sea_scaling2 = self.pion.params['strange_sea_scaling2'][0] #--this is a parameter that can be fitted that scales the strange quark in the sea down by some factor relative to the light sea quarks

            self.params['u1'][0]=4 + 2*strange_sea_scaling
            self.params['u1'][0]=(1-ubv-sv-g-(4+2*strange_sea_scaling2)*self.get_moments('u2',2))/self.get_moments('u1',2)
    
            self.params['db1'][0]=self.params['u1'][0]
            self.params['d1'][0] =self.params['u1'][0]
            self.params['sb1'][0]=self.params['u1'][0] * strange_sea_scaling
            self.params['sb2'][0]=self.params['u2'][0] * strange_sea_scaling2

            u  = self.get_moments('u1',2) + self.get_moments('u2',2)
            d  = self.get_moments('d1',2) + self.get_moments('d2',2)
            db = self.get_moments('db1',2) + self.get_moments('db2',2)
            sb = self.get_moments('sb1',2) + self.get_moments('sb2',2)
            
        
        up = 2*u + ubv
        dp = d + db
        sp = sv + 2*sb

        msr=g+up+dp+sp

        #--share
        self.sr={}
        self.sr['msr']=msr
        self.sr['ubv(1)'] = self.get_moments('ubv1',1) + self.get_moments('ubv2',1)
        self.sr['sv(1)']  = self.get_moments('sv1',1) + self.get_moments('sv2',1)
        self.sr['g(2)']   = g
        self.sr['light sea(2)'] = 2*u + dp
        self.sr['strange sea(2)'] = 2*sb
        self.sr['ub valence(2)']=ubv
        self.sr['s valence(2)']=sv

    def set_moms(self):

        moms={}

        moms['g']=self.get_moments('g1') + self.get_moments('g2')

        u   = self.get_moments('u1') + self.get_moments('u2')
        ubv = self.get_moments('ubv1') + self.get_moments('ubv2')
        ub  = ubv+u
        moms['up']=u + ub

        d  = self.get_moments('d1') + self.get_moments('d2')
        db = self.get_moments('db1') + self.get_moments('db2')
        moms['dp']=d+db

        sv = self.get_moments('sv1') + self.get_moments('sv2')
        sb = self.get_moments('sb1') + self.get_moments('sb2')
        s = sv + sb
        moms['sp']=s+sb

        moms['um']=u-ub
        moms['dm']=d-db
        moms['sm']=s-sb
        self.moms0=moms
        self.get_BC(moms)

    def setup(self):

        self.set_sumrules()
        self.set_moms()

        # we will store all Q2 values that has been precalc
        self.storage={}

    def beta(self,a,b):
        return gamma(a)*gamma(b)/gamma(a+b)

    def get_moments(self,flav,N=None):
        """
        if N==None: then parametrization is to be use to compute moments along mellin contour
        else the Nth moment is returned
        
        We are equating:
            1) kaon gluon = pion gluon (kaon fitting scenarios in [1,2,3,4])
            2) kaon sea quarks = pion sea quarks (kaon fitting scenarios in [1,2,3])
            3) kaon s_valence = pi total valence - kaon ubv_valence (kaon fitting scenario 1)
        """
 
        if flav not in ['ubv1','sv1','d1','ubv2','sv2','d2'] and conf['kaon fitting scenario'] in [1,2,3]:
            if N==None:
                return self.pion.get_moments(flav)
            else:
                return self.pion.get_moments(flav,N)

        elif 'g' in flav and conf['kaon fitting scenario'] in [1,2,3,4]:
            if N==None:
                return self.pion.get_moments(flav)
            else:
                return self.pion.get_moments(flav,N)
        
        elif flav == 'd1' and conf['kaon fitting scenario'] in [1,2,3]:
            if N==None:
                return self.pion.get_moments('u1')
            else:
                return self.pion.get_moments('u1',N)
        
        elif flav == 'd2' and conf['kaon fitting scenario'] in [1,2,3]:
            if N==None:
                return self.pion.get_moments('u2')
            else:
                return self.pion.get_moments('u2',N)
        
        elif flav == 'sv1' and conf['kaon fitting scenario']==1:
            if N==None:
                pi_val = self.pion.get_moments('ubv1') + self.pion.get_moments('dv1')
                return pi_val - self.get_moments('ubv1')
            else:
                pi_val = self.pion.get_moments('ubv1',N) + self.pion.get_moments('dv1',N)
                return pi_val - self.get_moments('ubv1',N)
        
        elif flav == 'sv2' and conf['kaon fitting scenario']==1:
            if N==None:
                pi_val = self.pion.get_moments('ubv2') + self.pion.get_moments('dv2')
                return pi_val - self.get_moments('ubv2')
            else:
                pi_val = self.pion.get_moments('ubv2',N) + self.pion.get_moments('dv2',N)
                return pi_val - self.get_moments('ubv2',N)
        
        else:
        
            if N==None: N=self.mellin.N
            M,a,b,c,d=self.params[flav]
            if 'pdf-pion valence parametrization' in conf:
                valpar = conf['pdf-pion valence parametrization']
            else:
                valpar = 'c*x**2'
            if 'ubv' in flav and valpar=='c*x**2':
            # if False:
                norm=self.beta(2+a,b+1)+c*self.beta(2+a+d,b+1)
                mom=self.beta(N+a,b+1)+c*self.beta(N+a+d,b+1)
            else:
                norm=self.beta(2+a,b+1)+c*self.beta(2+a+0.5,b+1)+d*self.beta(2+a+1.0,b+1)
                mom=self.beta(N+a,b+1)+c*self.beta(N+a+0.5,b+1)+d*self.beta(N+a+1.0,b+1)
            return M*mom/norm

    def _get_BC(self,g,up,um,dp,dm,sp,sm,cp,cm,bp,bm,tp,tm):
        N=self.mellin.N

        # flav composition
        vm,vp={},{}
        vm[35]= bm + cm + dm + sm - 5*tm + um
        vm[24]= -4*bm + cm + dm + sm + um 
        vm[15]= -3*cm + dm + sm + um
        vm[8] = dm - 2*sp + 2*(-sm + sp) + um
        vm[3] = -dm + um
        vm[0] = np.zeros(N.size,dtype=complex)
        vp[0] = np.zeros(N.size,dtype=complex)
        vp[3] = -dp + up
        vp[8] = dp - 2*sp + up 
        vp[15]= -3*cp + dp + sp + up 
        vp[24]= -4*bp + cp + dp + sp + up 
        vp[35]= bp + cp + dp + sp - 5*tp + up 
        qs    = bp + cp + dp + sp + tp + up 
        qv    = bm + cm + dm + sm + tm + um 
        q     = np.zeros((2,N.size),dtype=complex)
        q[0]=np.copy(qs)
        q[1]=np.copy(g)

        BC={}
        BC['vm']=vm 
        BC['vp']=vp 
        BC['qv']=qv
        BC['q'] =q
        return BC

    def get_state(self):
        return (self.BC3,self.BC4,self.BC5)

    def set_state(self,state):
        self.BC3, self.BC4, self.BC5 = state[:]
        if 'erase pdf-kaon storage state' in conf and conf['erase pdf-kaon storage state']==False:
            pass
        else: self.storage = {}

    def get_BC(self,moms):

        N=self.mellin.N
        zero=np.zeros(N.size,dtype=complex)

        ###############################################
        # BC for Nf=3
        g   = moms['g']
        up  = moms['up']
        um  = moms['um']
        dp  = moms['dp']
        dm  = moms['dm']
        sp  = moms['sp']
        sm  = moms['sm']
        cp  = zero
        cm  = zero
        bp  = zero
        bm  = zero
        self.BC3=self._get_BC(g,up,um,dp,dm,sp,sm,zero,zero,zero,zero,zero,zero)

        ###############################################
        # BC for Nf=4
        BC4=self.dglap.evolve(self.BC3,self.Q20,self.mc2,3)
        g =BC4['g']
        up=BC4['up']
        dp=BC4['dp']
        sp=BC4['sp']
        cp=BC4['cp']
        bp=BC4['bp']
        tp=BC4['tp']
        um=BC4['um']
        dm=BC4['dm']
        sm=BC4['sm']
        cm=BC4['cm']
        bm=BC4['bm']
        tm=BC4['tm']
        self.BC4=self._get_BC(g,up,um,dp,dm,sp,sm,cp,cm,bp,bm,tp,tm)

        ###############################################
        # BC for Nf=5
        BC5=self.dglap.evolve(self.BC4,self.mc2,self.mb2,4)
        g =BC5['g']
        up=BC5['up']
        dp=BC5['dp']
        sp=BC5['sp']
        cp=BC5['cp']
        bp=BC5['bp']
        tp=BC5['tp']
        um=BC5['um']
        dm=BC5['dm']
        sm=BC5['sm']
        cm=BC5['cm']
        bm=BC5['bm']
        tm=BC5['tm']
        self.BC5=self._get_BC(g,up,um,dp,dm,sp,sm,cp,cm,bp,bm,tp,tm)

    def evolve(self,Q2):

        if Q2 not in self.storage:
            if self.mb2<Q2: 
                self.storage[Q2]=self.dglap.evolve(self.BC5,self.mb2,Q2,5)
            elif self.mc2<=Q2 and Q2<=self.mb2: 
                self.storage[Q2]=self.dglap.evolve(self.BC4,self.mc2,Q2,4)
            elif Q2<self.mc2: 
                self.storage[Q2]=self.dglap.evolve(self.BC3,self.Q20,Q2,3)

    def get_xF(self,x,Q2,flav,evolve=True):
        if evolve: self.evolve(Q2)
        #return x*conf['mellin-pion'].invert(x,self.storage[Q2][flav])
        return x*self.mellin.invert(x,self.storage[Q2][flav])

    def check_positivity(self):

        X = np.linspace(0.001,0.9,100)
        if 'Q20 pos' in conf: Q2 = conf['Q20 pos']
        else:                 Q2 = conf['Q20']

        _PDF = conf['pdf-kaon']

        violations = np.zeros(len(X))
        _pdf_sv = lambda _x: self.get_xF(_x,Q2,'s')/_x - self.get_xF(_x,Q2,'sb')/_x
        pdf_sv  = np.array([_pdf_sv(x)  for x in X])
        _pdf_ubv = lambda _x: self.get_xF(_x,Q2,'ub')/_x - self.get_xF(_x,Q2,'u')/_x
        pdf_ubv  = np.array([_pdf_ubv(x)  for x in X])

        for i in range(len(X)):
            if pdf_sv[i]  < 0.0: violations[i] += pdf_sv[i]
            if pdf_ubv[i]  < 0.0: violations[i] += pdf_ubv[i]

        return violations*150.0

    #--generate report for positivity constraints
    def gen_report(self,verb=1,level=1):
          
        L=[]

        if 'positivity_constraints' in conf and conf['positivity_constraints']\
        or 'positivity constraints' in conf and conf['positivity constraints']:
            res2 = conf['positivity chi2']
            L.append('chi2 from positivity constraints: %3.5f'%res2)

        return L

    def get_xF0(self,x,flav,evolve=True):
        if flav=='um': mom=self.moms0['um']
        elif flav=='dm': mom=self.moms0['dm']
        elif flav=='sm': mom=self.moms0['sm']
        #return x*conf['mellin-pion'].invert(x,mom)
        return x*self.mellin.invert(x,mom)

    def get_second_moment(self,flav,Q2):
        self.evolve(Q2)
        if flav=='g': 
            f=lambda x: self.get_xF(x,Q2,'g',evolve=False)
        if flav=='sea': 
            f=lambda x: 2*self.get_xF(x,Q2,'u',evolve=False)\
                     +2*self.get_xF(x,Q2,'db',evolve=False)\
                     +self.get_xF(x,Q2,'s',evolve=False)\
                     +self.get_xF(x,Q2,'sb',evolve=False)
        if flav=='valence':
            f=lambda x: self.get_xF(x,Q2,'ub',evolve=False)-self.get_xF(x,Q2,'u',evolve=False)\
                     +self.get_xF(x,Q2,'d',evolve=False) -self.get_xF(x,Q2,'db',evolve=False)
        return quad(f,0,1)[0]
