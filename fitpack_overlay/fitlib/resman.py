import sys, os
import time
import numpy as np

#--from nuclib
import nuclib

#--from qcdlib
from qcdlib import pdf,ppdf
from qcdlib import tpdf
from qcdlib import ffpion, ffkaon
from qcdlib import ffsum
from qcdlib import pdfpion,pdfkaon
from qcdlib import pdfpionalt,nucpdfalt,nucpdfaltdm
from qcdlib import tmd_QZ as tmdqz
from qcdlib import tmd_ZETA as tmdzeta
from qcdlib import tmd_CSS as tmdcss
from qcdlib import tmd_CSS1 as tmdcss1
from qcdlib import tmd_NP as tmdnp # collecton of NP functions
#from qcdlib import wpdf
#from qcdlib import tmdpdf
#from qcdlib import diff
from qcdlib import g1T
from qcdlib import aux, eweak, alphaS, mellin

#--from fitlib
from fitlib.parman import PARMAN

#--from tools
from tools.tools    import checkdir
from tools.config   import conf,load_config,options
from tools.parallel import PARALLEL

#--from nuclib
from nuclib import deuterium
from nuclib import helium


#--from obslib
import obslib.idis_V0.residuals as idis_residuals_V0 
import obslib.idis_V0.reader    as idis_reader_V0 
import obslib.idis_V0.theory    as idis_theory_V0

import obslib.pidis_V0.residuals as pidis_residuals_V0
import obslib.pidis_V0.reader    as pidis_reader_V0
import obslib.pidis_V0.theory    as pidis_theory_V0

import obslib.sidis_V0.residuals as sidis_residuals_V0
import obslib.sidis_V0.reader    as sidis_reader_V0
import obslib.sidis_V0.theory    as sidis_theory_V0

import obslib.psidis_V0.residuals as psidis_residuals_V0
import obslib.psidis_V0.reader    as psidis_reader_V0
import obslib.psidis_V0.theory    as psidis_theory_V0

import obslib.idis.residuals as idis_residuals
import obslib.idis.reader    as idis_reader 
import obslib.idis.theory    as idis_theory
import obslib.idis.theory_xsec    as idis_theory_xsec

import obslib.pidis.residuals as pidis_residuals
import obslib.pidis.reader    as pidis_reader
import obslib.pidis.theory    as pidis_theory

import obslib.sidis.residuals as sidis_residuals
import obslib.sidis.residuals_xsec as sidis_residuals_xsec
import obslib.sidis.reader    as sidis_reader
import obslib.sidis.theory    as sidis_theory
import obslib.sidis.theory_xsec    as sidis_theory_xsec

import obslib.psidis.residuals as psidis_residuals
import obslib.psidis.reader    as psidis_reader
import obslib.psidis.theory    as psidis_theory


import obslib.SU23.residuals
import obslib.SU23.reader


import obslib.dy.residuals
import obslib.dy.reader
import obslib.dy.mellin

import obslib.zrap.residuals
import obslib.zrap.reader
import obslib.zrap.mellin

import obslib.wasym.residuals
import obslib.wasym.reader
import obslib.wasym.mellin

import obslib.wzrv.residuals
import obslib.wzrv.reader
import obslib.wzrv.mellin

import obslib.jet.residuals
import obslib.jet.reader
import obslib.jet.theory

import obslib.pjet.residuals
import obslib.pjet.reader
import obslib.pjet.theory

import obslib.pp_hadron.residuals
import obslib.pp_hadron.reader
import obslib.pp_hadron.theory

import obslib.sia.residuals
import obslib.sia.reader
import obslib.sia.theory

import obslib.sia_V0.residuals
import obslib.sia_V0.reader
import obslib.sia_V0.theory

import obslib.dypion.residuals
import obslib.dypion.reader
import obslib.dypion.theory
import obslib.dypion.dyres
import obslib.dypion.dyresmellin

import obslib.LeadingBaryon.residuals
import obslib.LeadingBaryon.reader
import obslib.LeadingBaryon.theory
import obslib.pion_qT.new_y_theory

import obslib.DYpT.residuals
import obslib.DYpT.reader
import obslib.DYpT.fo

import obslib.wc.residuals
import obslib.wc.reader
import obslib.wc.applgrid

import obslib.RpITD.residuals
import obslib.RpITD.reader
import obslib.RpITD.theory

import obslib.ccLCS.residuals
import obslib.ccLCS.reader
import obslib.ccLCS.theory

import obslib.dy_qT.residuals
import obslib.dy_qT.reader
import obslib.dy_qT.theory

import obslib.pp_BX.residuals
import obslib.pp_BX.reader
import obslib.pp_BX.theory

#--LQCD data for pseudo-ITD
import obslib.lqcd_itd.residuals
import obslib.lqcd_mom.residuals
import obslib.lqcd_mom.reader
import obslib.lqcd_mom.theory


class RESMAN:

    def __init__(self,nworkers=2,parallel=True,datasets=True,parman=None):

        #--default to new idis version
        if   'dis version' in conf: self.dis_version = conf['dis version']
        elif 'dis_version' in conf: self.dis_version = conf['dis_version']
        else:                       self.dis_version = 1

        #--default to new sia version
        if   'sia version' in conf: self.sia_version = conf['sia version']
        elif 'sia_version' in conf: self.sia_version = conf['sia_version']
        else:                       self.sia_version = 1
        
        self.setup_core()
        if parman==None: self.parman=PARMAN()
        else: self.parman=parman

        #--if pdf in active distributions, update unpolarized observables
        #--otherwise, unpolarized observables are only calculated once at beginning of fit
        self.update_unpol = True
        if ('steps' in conf) and (len(conf['steps']) > 0):
            istep = sorted(conf['steps'].keys())[-1]
            if   'pdf'     in conf['steps'][istep]['active distributions']: self.update_unpol = True
            elif 'eweak'   in conf['steps'][istep]['active distributions']: self.update_unpol = True
            elif 'ht4'     in conf['steps'][istep]['active distributions']: self.update_unpol = True
            elif 'off pdf' in conf['steps'][istep]['active distributions']: self.update_unpol = True
            else:                                                           self.update_unpol = False

        #--if doing predictions, need to update unpolarized observables
        if 'predict' in conf and conf['predict']: self.update_unpol = True

        
        if self.dis_version==0: print('USING DIS VERSION: %s'%self.dis_version)
        if self.sia_version==0: print('USING SIA VERSION: %s'%self.sia_version)

        if datasets:
            if 'idis'        in conf['datasets']: self.setup_idis()
            if 'pidis'       in conf['datasets']: self.setup_pidis()
            if 'SU23'        in conf['datasets']: self.setup_SU23()
            if 'sidis'       in conf['datasets']: self.setup_sidis()
            if 'psidis'      in conf['datasets']: self.setup_psidis()
            if 'dy'          in conf['datasets']: self.setup_dy()
            if 'zrap'        in conf['datasets']: self.setup_zrap()
            if 'wasym'       in conf['datasets']: self.setup_wasym()
            if 'wzrv'        in conf['datasets']: self.setup_wzrv()
            if 'jet'         in conf['datasets']: self.setup_jet()
            if 'pjet'        in conf['datasets']: self.setup_pjet()
            if 'pp_hadron'   in conf['datasets']: self.setup_pp_hadron()
            if 'sia'         in conf['datasets']: self.setup_sia()
            if 'dy-pion'     in conf['datasets']: self.setup_dy_pion()
            if 'ln'          in conf['datasets']: self.setup_ln()
            if 'pion_qT'     in conf['datasets']: self.setup_pion_qT()
            if 'lqcd_mom'    in conf['datasets']: self.setup_lqcd_mom()
            if 'DYpT'        in conf['datasets']: self.setup_DYpT()
            if 'wc'          in conf['datasets']: self.setup_wc()
            if 'RpITD'       in conf['datasets']: self.setup_RpITD()
            if 'ccLCS'       in conf['datasets']: self.setup_ccLCS()
            if 'dy_qT'       in conf['datasets']: self.setup_dy_qT()
            if 'pp_BX'       in conf['datasets']: self.setup_pp_BX()

        self.parallel_mode=False

        if parallel==True:
            #--if only idis/pidis/sia/sidis being fit, always turn parallel off
            noparallel = ['idis','pidis','sia','sidis','psidis','SU23','wc','pp_BX']
            for exp in conf['datasets']:
                if exp in noparallel:
                    continue
                elif (exp not in noparallel) and (len(conf['datasets'][exp]['xlsx']) > 0):
                    self.parallel_mode=parallel

        if self.parallel_mode:
            self.setup_parallel(nworkers)
        else:
            self.nworkers=1

    def setup_core(self):

        conf['aux']          = aux.AUX()
        conf['mellin']       = mellin.MELLIN(npts=4)
        conf['mellin-pion']  = mellin.MELLIN(npts=8,extended=True)
        conf['dmellin'] = mellin.DMELLIN(nptsN=4,nptsM=4)
        conf['alphaS']  = alphaS.ALPHAS()
        conf['eweak']   = eweak.EWEAK()

        #--type of nuclear smearing to use
        #--overwrite
        if 'dsmf_type' not in conf: conf['dsmf_type'] = 'paris'
        if 'hsmf_type' not in conf: conf['hsmf_type'] = 'kpsv'
        
        #--load tables
        dsmf_path='%s/nuclib/grids/deuteron'%os.environ['FITPACK']
        hsmf_path='%s/nuclib/grids/helium'%os.environ['FITPACK']
        conf['dsmf'] = deuterium.DEUTERON(dsmf_path,dsmf_type = conf['dsmf_type'])
        conf['hsmf'] = helium.HELIUM(path2nuctab=hsmf_path,group = conf['hsmf_type'])

        if 'pdf' in conf['params']:

            conf['pdf']     = pdf.PDF()
            conf['pdf-mom'] = pdf.PDF(mellin.IMELLIN())
            conf['pdfA']    = conf['pdf']
            conf['pdfB']    = conf['pdf']
            conf['pdf-8']   = pdf.PDF(mellin.MELLIN(npts=8))

        if 'ppdf' in conf['params']:

            conf['ppdf']     = ppdf.PPDF()
            conf['ppdf-mom'] = ppdf.PPDF(mellin.IMELLIN())
            conf['ppdfA']    = conf['ppdf']
            conf['ppdfB']    = conf['ppdf']

        if 'tpdf' in conf['params']:

            conf['tpdf']     = tpdf.TPDF()
            conf['tpdf-mom'] = tpdf.TPDF(mellin.IMELLIN())
            conf['tpdfA']    = conf['tpdf']
            conf['tpdfB']    = conf['tpdf']


        #if 't3ppdf' in conf['params']:
        #    conf['t3ppdf'] = obslib.pidis.theory.t3ppdf()

        if 'ht4' in conf['params']:

            if self.dis_version==0: conf['ht4']      = obslib.idis_V0.theory.HT()
            if self.dis_version==1: conf['ht4']      = obslib.idis.theory.HT()

        if 'pol ht4' in conf['params']:

            if self.dis_version==0: conf['pol ht4']  = obslib.idis_V0.theory.HT()
            if self.dis_version==1: conf['pol ht4']  = obslib.idis.theory.HT()

        if self.dis_version==0:
            if 'g2res' in conf['params']:
                if 'g2resN' in conf: conf['g2res']    = obslib.pidis_V0.theory.g2res(conf['g2resN'])
                else:                conf['g2res']    = obslib.pidis_V0.theory.g2res(5)
        if self.dis_version==1:
            if 'gXres' in conf['params']:
                conf['gXres']    = obslib.pidis.theory.gXres()

        if 'off pdf' in conf['params']:

            conf['off pdf'] = pdf.OFFSHELL_PDF()

        if 'off pdf0' in conf['params']:

            conf['off pdf0'] = pdf.OFFSHELL_PDF()


        if 'off pdf1' in conf['params']:

            conf['off pdf1'] = pdf.OFFSHELL_PDF()


        if 'ffpion' in conf['params']:

            try:    conf['ffpion']= ff.FF()
            except: conf['ffpion']= ffpion.FF()

        if 'ffkaon' in conf['params']:

            try:    conf['ffkaon']= ff.FF()
            except: conf['ffkaon']= ffkaon.FF()

        if 'ffhadron' in conf['params']:

            conf['ffhadron']= ffsum.FF()

        if 'g1T' in conf['params']:
            conf['g1T'] = g1T.G1T()

        if 'pdf-pion' in conf['params']:

            #conf['Z']=74. #--Tungsten
            #conf['A']=184. #--Tungsten
            if 'pion resummation' in conf and conf['pion resummation']:
                if conf['MP method']=='cosine' or conf['MP method']=='expansion':
                    conf['dymell1']=obslib.dypion.dyresmellin.MFMELLIN(1)
                    conf['dymell2']=obslib.dypion.dyresmellin.MFMELLIN(2)
                    conf['dymell3']=obslib.dypion.dyresmellin.MFMELLIN(3)
                    conf['pdf-W']=nucpdfalt.WPDF()
                    conf['pdf-pion']=pdfpionalt.PDF()
                elif conf['MP method']=='double mellin':
                    conf['pdf-pion']=pdfpion.PDF()
                    conf['pdf-pion-mom']=pdfpion.PDF(mellin.IMELLIN())
            else:
                conf['pdf-pion']=pdfpion.PDF()
                conf['pdf-pion-mom']=pdfpion.PDF(mellin.IMELLIN())

        if 'pdf-kaon' in conf['params']:
            conf['pdf-kaon'] = pdfkaon.PDF()
            conf['pdf-kaon-mom']=pdfkaon.PDF(mellin.IMELLIN())

        if 'RpITD' in conf['datasets']:

            imell=mellin.IMELLIN()
            imell.N=np.array([])
            for i in range(1,51):
                iN=np.array([i],dtype=complex)
                imell.N=np.append(imell.N,iN)
            conf['imell']=imell
            conf['pdf-pion-int']=pdfpion.PDF(mellin=imell)

         #if 'tmd' in conf['params']:

             #conf['tmd']=tmdpdf.TMDPDF(conf['pdf'])
             #conf['pdf-W']=wpdf.PDF()
             #conf['tmd-W']=tmdpdf.TMDPDF(conf['pdf-W'],mellin=conf['mellin-pion'])

         #if 'tmd-pion' in conf['params']:

             #conf['tmd-pion']=tmdpdf.TMDPDF(conf['pdf-pion'],mellin=conf['mellin-pion'])

        if 'np tmd' in conf['params']:
            if conf['tmd settings']['method'] and conf['tmd settings']['method'] in ['QZ','QZ_W']:
                
                conf['tmdmodel'] = tmdqz.MODEL()
                conf['pert evo'] = tmdqz.PERTURBATIVE_EVOLUTION()
                #conf['np tmd universal'] = tmdqz.NONPERTURBATIVE_UNIVERSAL()
                conf['np tmd universal'] = tmdnp.NONPERTURBATIVE_QZ_UNIVERSAL()
                
                if conf['tmd settings']['parametrization']['flavor']:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic']=='cosh':
                        #conf['np tmd'] = tmdqz.NONPERTURBATIVE_FLAV_COSH()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_COSH_FLAV()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic']=='MAP':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP_FLAV()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic']=='ART':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
                    else:
                        """
                        We should rewrite this!
                        """
                        conf['np tmd'] = tmdqz.NONPERTURBATIVE_FLAV()
                        #conf['np tmd'] = tmdnp.NONPERTURBATIVE_COSH_FLAV()
                else:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'cosh':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_COSH()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
                    else:
                        """
                        We should rewrite this!
                        """
                        conf['np tmd'] = tmdqz.NONPERTURBATIVE()
                        #conf['np tmd'] = tmdnp.NONPERTURBATIVE_COSH()
                    
            #            elif 'tmd method' in conf and conf['tmd method'] == 'ZETA':
            #                
            #                conf['tmdmodel'] = tmdzeta.MODEL()
            #                conf['pert evo'] = tmdzeta.PERTURBATIVE_EVOLUTION()
            #                
            #                #conf['np tmd universal']   = tmdzeta.NONPERTURBATIVE_UNIVERSAL()
            #                #default
            #                conf['np tmd universal']  = tmdnp.NONPERTURBATIVE_ART_UNIVERSAL()
            #                    
            #                if 'tmd parametrization' in conf and conf['tmd parametrization']=='flavor':
            #                    #conf['np tmd'] = tmdzeta.NONPERTURBATIVE_FLAV()
            #                    conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
            #                else:
            #                    if 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'MAP': #- do not forget to set conf['np tmd aramerization']
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
            #                    elif 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'ART': #- do not forget to set conf['np tmd aramerization']
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
            #                    else:
            #                    #conf['np tmd'] = tmdzeta.NONPERTURBATIVE()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART() # Default
                                
            #            elif 'tmd method' in conf and conf['tmd method'] == 'CSS':
            #                
            #                conf['tmdmodel'] = tmdcss.MODEL()
            #                conf['pert evo'] = tmdcss.PERTURBATIVE_EVOLUTION()
            #                #conf['np tmd universal']   = tmdcss.NONPERTURBATIVE_UNIVERSAL()
            #                #default
            #                conf['np tmd universal']   = tmdnp.NONPERTURBATIVE_MAP_UNIVERSAL()
            #
            #                
            #                if 'tmd parametrization' in conf and conf['tmd parametrization']=='flavor':
            #                    if 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'MAP':
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_FLAV()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP_FLAV()
            #                    elif 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'ART':
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART_FLAV()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
            #                    else:
            #                        print("resman.py 342: Specify 'conf['np tmd paramerization']'")
            #                else:
            #                    if 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'MAP': #- do not forget to set conf['np tmd aramerization']
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
            #                    elif 'np tmd paramerization' in conf and conf['np tmd paramerization'] == 'ART': #- do not forget to set conf['np tmd aramerization']
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART()
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
            #                    else:
            #                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE() # this is the default parametrization for CSS (= MAP), see also parman.py set_nptmd_params(self)
            #                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP() # this is the default parametrization for CSS (= MAP), see also parman.py set_nptmd_params(self)
            #
            elif conf['tmd settings']['method'] and conf['tmd settings']['method'] == 'ZETA':
                
                conf['tmdmodel'] = tmdzeta.MODEL()
                conf['pert evo'] = tmdzeta.PERTURBATIVE_EVOLUTION()
                
                #conf['np tmd universal']   = tmdzeta.NONPERTURBATIVE_UNIVERSAL()
                #default
                conf['np tmd universal']  = tmdnp.NONPERTURBATIVE_ART_UNIVERSAL()
                    
                if conf['tmd settings']['parametrization']['flavor']:
                    #conf['np tmd'] = tmdzeta.NONPERTURBATIVE_FLAV()
                    conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
                else:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP': #- do not forget to set conf['np tmd aramerization']
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART': #- do not forget to set conf['np tmd aramerization']
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
                    else:
                    #conf['np tmd'] = tmdzeta.NONPERTURBATIVE()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART() # Default
            elif conf['tmd settings']['method'] and conf['tmd settings']['method'] == 'CSS':
                
                conf['tmdmodel'] = tmdcss.MODEL()
                conf['pert evo'] = tmdcss.PERTURBATIVE_EVOLUTION()
                #conf['np tmd universal']   = tmdcss.NONPERTURBATIVE_UNIVERSAL()
                #default
                conf['np tmd universal']   = tmdnp.NONPERTURBATIVE_MAP_UNIVERSAL() # This class will have various implementations of gK, see tmd_NP.py

                
                if conf['tmd settings']['parametrization']['flavor']:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP':
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_FLAV()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP_FLAV()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART':
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART_FLAV()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
                    else:
                        print("resman.py 342: Specify 'conf['np tmd parametrization']'")
                else:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP': #- do not forget to set conf['np tmd aramerization']
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART': #- do not forget to set conf['np tmd aramerization']
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE_ART()
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
                    else:
                        #conf['np tmd'] = tmdcss.NONPERTURBATIVE() # this is the default parametrization for CSS (= MAP), see also parman.py set_nptmd_params(self)
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP() # this is the default parametrization for CSS (= MAP), see also parman.py set_nptmd_params(self)
            elif conf['tmd settings']['method'] and conf['tmd settings']['method'] == 'CSS1': # No H in CSS1
                
                conf['tmdmodel'] = tmdcss1.MODEL()
                conf['pert evo'] = tmdcss1.PERTURBATIVE_EVOLUTION()
                conf['np tmd universal']   = tmdnp.NONPERTURBATIVE_MAP_UNIVERSAL() 

                
                if conf['tmd settings']['parametrization']['flavor']:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP_FLAV()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART':
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART_FLAV()
                    else:
                        print("resman.py 342: Specify 'conf['np tmd paramerization']'")
                else:
                    if conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'MAP': 
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP()
                    elif conf['tmd settings']['parametrization']['intrinsic'] and conf['tmd settings']['parametrization']['intrinsic'] == 'ART': 
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_ART()
                    else:
                        conf['np tmd'] = tmdnp.NONPERTURBATIVE_MAP() # this is the default parametrization for CSS1 (= MAP), see also parman.py set_nptmd_params(self)                        
            else: 
                print("resman.py 350: Specify 'conf['tmd method']'")
                sys.exit()


        if   'lqcd_itd' in conf: self.lqcd_itd = conf['lqcd_itd']
        elif 'lqcd itd' in conf: self.lqcd_itd = conf['lqcd itd']
        else:                    self.lqcd_itd = False

        if self.lqcd_itd: self.lqcd_itd_res=obslib.lqcd_itd.residuals.RESIDUALS()

    #--observables

    def setup_idis(self):

        if self.dis_version==0:
            self.idis_tabs=idis_reader_V0.READER().load_data_sets('idis')
            conf['idis tabs'] = self.idis_tabs
            self.idis_thy=idis_theory_V0.THEORY()
            conf['idis'] = self.idis_thy
            self.idis_res=idis_residuals_V0.RESIDUALS()

        if self.dis_version==1:
            self.idis_tabs=idis_reader.READER().load_data_sets('idis')
            conf['idis tabs'] = self.idis_tabs
            self.idis_thy=idis_theory.THEORY()
            conf['idis'] = self.idis_thy
            self.idis_res=idis_residuals.RESIDUALS()
            if 'sidis_integration' in conf and conf['sidis_integration'] == True: conf['sidis_xsec'] = True
            if 'sidis_xsec' in conf and conf['sidis_xsec'] == True:
                self.idis_thy_xsec=obslib.idis.theory_xsec.THEORY()
                conf['idis_xsec'] = self.idis_thy_xsec

    def setup_pidis(self):

        if 'idis' not in conf['datasets']:
            conf['datasets']['idis'] = {_:{} for _ in ['xlsx','norm']}
            self.setup_idis()

        if self.dis_version==0:
            self.pidis_tabs=obslib.pidis_V0.reader.READER().load_data_sets('pidis')
            conf['pidis tabs'] = self.pidis_tabs
            self.pidis_thy=obslib.pidis_V0.theory.THEORY()
            conf['pidis'] = self.pidis_thy
            self.pidis_res=obslib.pidis_V0.residuals.RESIDUALS()
        if self.dis_version==1:
            self.pidis_tabs=obslib.pidis.reader.READER().load_data_sets('pidis')
            conf['pidis tabs'] = self.pidis_tabs
            self.pidis_thy=obslib.pidis.theory.THEORY()
            conf['pidis'] = self.pidis_thy
            self.pidis_res=obslib.pidis.residuals.RESIDUALS()
        #--calculate unpolarized observable once at start of fit
        if self.update_unpol==False and self.dis_version==0: self.idis_thy._update()

    def setup_SU23(self):

        self.SU23_tabs=obslib.SU23.reader.READER().load_data_sets('SU23')
        conf['SU23 tabs'] = self.SU23_tabs
        self.SU23_res=obslib.SU23.residuals.RESIDUALS()

    def setup_sidis(self):

        if 'idis' not in conf['datasets']:
            conf['datasets']['idis'] = {_:{} for _ in ['xlsx','norm']}
            self.setup_idis()

        if self.dis_version==0:
            self.sidis_tabs=obslib.sidis_V0.reader.READER().load_data_sets('sidis')
            conf['sidis tabs'] = self.sidis_tabs
            self.sidis_thy=obslib.sidis_V0.theory.THEORY()
            conf['sidis'] = self.sidis_thy
            self.sidis_res=obslib.sidis_V0.residuals.RESIDUALS()
            #--calculate unpolarized observable once at start of fit
            if self.update_unpol==False: self.idis_thy._update()
        if self.dis_version==1:
            self.sidis_tabs=obslib.sidis.reader.READER().load_data_sets('sidis')
            conf['sidis tabs'] = self.sidis_tabs
            if 'sidis_integration' in conf and conf['sidis_integration'] == True: conf['sidis_xsec'] = True
            if 'sidis_xsec' in conf and conf['sidis_xsec'] == True: 
                self.sidis_thy_xsec=obslib.sidis.theory_xsec.THEORY()
                conf['sidis'] = self.sidis_thy_xsec
                self.sidis_res=obslib.sidis.residuals_xsec.RESIDUALS()
            else:                 
                self.sidis_thy=obslib.sidis.theory.THEORY()
                conf['sidis'] = self.sidis_thy
                self.sidis_res=obslib.sidis.residuals.RESIDUALS()

    def setup_psidis(self):

        if 'pidis' not in conf['datasets']:
            conf['datasets']['pidis'] = {_:{} for _ in ['xlsx','norm']}
            self.setup_pidis()
        if 'sidis' not in conf['datasets']:
            conf['datasets']['sidis'] = {_:{} for _ in ['xlsx','norm']}
            self.setup_sidis()

        if self.dis_version==0:
            self.psidis_tabs=obslib.psidis_V0.reader.READER().load_data_sets('psidis')
            conf['psidis tabs'] = self.psidis_tabs
            self.psidis_thy=obslib.psidis_V0.theory.THEORY()
            conf['psidis'] = self.psidis_thy
            self.psidis_res=obslib.psidis_V0.residuals.RESIDUALS()
            #--calculate unpolarized observable once at start of fit
            if self.update_unpol==False: self.idis_thy._update()
        if self.dis_version==1:
            self.psidis_tabs=obslib.psidis.reader.READER().load_data_sets('psidis')
            conf['psidis tabs'] = self.psidis_tabs
            self.psidis_thy=obslib.psidis.theory.THEORY()
            conf['psidis'] = self.psidis_thy
            self.psidis_res=obslib.psidis.residuals.RESIDUALS()

    def setup_dy(self):
        self.dy_tabs=obslib.dy.reader.READER().load_data_sets('dy')
        conf['dy tabs'] = self.dy_tabs
        conf['path2dytab']='%s/dy'%conf['grid path']
        self.dy_thy=obslib.dy.mellin.MELLIN(parallel=True)
        self.dy_thy.setup_interpolation()
        conf['dy'] = self.dy_thy
        self.dy_res=obslib.dy.residuals.RESIDUALS()

    def setup_zrap(self):

        self.zrap_tabs=obslib.zrap.reader.READER().load_data_sets('zrap')
        conf['zrap tabs'] = self.zrap_tabs
        conf['path2zraptab']='%s/zrap'%conf['grid path']
        self.zrap_thy=obslib.zrap.mellin.MELLIN(parallel=True)
        conf['zrap'] = self.zrap_thy
        self.zrap_res=obslib.zrap.residuals.RESIDUALS()

    def setup_wasym(self):

        self.wasym_tabs=obslib.wasym.reader.READER().load_data_sets('wasym')
        conf['wasym tabs'] = self.wasym_tabs
        conf['path2wasymtab']='%s/wasym'%conf['grid path']
        self.wasym_thy=obslib.wasym.mellin.MELLIN(parallel=True)
        conf['wasym'] = self.wasym_thy
        self.wasym_res=obslib.wasym.residuals.RESIDUALS()

    def setup_wzrv(self):

        self.wzrv_tabs=obslib.wzrv.reader.READER().load_data_sets('wzrv')
        conf['wzrv tabs'] = self.wzrv_tabs
        conf['path2wzrvtab']='%s/wzrv'%conf['grid path']
        self.wzrv_thy=obslib.wzrv.mellin.MELLIN(parallel=True)
        conf['wzrv'] = self.wzrv_thy
        self.wzrv_res=obslib.wzrv.residuals.RESIDUALS()

        #--calculate unpolarized observable once at start of fit
        if self.update_unpol==False:
            tasks = self.wzrv_thy.get_tasks()
            for task in tasks:
                if task['pol']==True: continue
                self.wzrv_thy.process_request(task)

    def setup_jet(self):
        if 'jet' not in conf:
            conf['jet'] = obslib.jet.theory.THEORY()
        conf['jet tabs'] = obslib.jet.reader.READER().load_data_sets('jet')
        conf['jet'].setup_interpolation()
        conf['jet grid'] = '%s/jet' % conf['grid path']
        conf['jet'].load_mtab_interp()
        self.jet_res = obslib.jet.residuals.RESIDUALS()

    def setup_pjet(self):
        if 'jet' not in conf:
            conf['jet'] = obslib.jet.theory.THEORY()
        conf['pjet'] = obslib.pjet.theory.THEORY()
        conf['pjet tabs'] = obslib.pjet.reader.READER().load_data_sets('pjet')
        conf['pjet'].setup_interpolation()
        conf['jet grid'] = '%s/jet' % conf['grid path']
        conf['pjet grid'] = '%s/pjet' % conf['grid path']
        conf['jet'].interpolation_indices = conf['pjet'].interpolation_indices
        conf['jet'] .load_mtab_interp()
        conf['pjet'].load_mtab_interp()
        self.pjet_res = obslib.pjet.residuals.RESIDUALS()

        if self.update_unpol == False:
            for _ in range(len(conf['pjet'].tasks)):
                conf['pjet'].tasks[_] = conf['pjet'].process_request(conf['pjet'].tasks[_])
                conf['pjet'].tasks[_]['update_unpol'] = False
                conf['pjet'].update(conf['pjet'].tasks[_])

    def setup_pp_hadron(self):
        truncate = 4
        interpolate = True
        if 'pp_hadron' not in conf:
            conf['pp_hadron'] = obslib.pp_hadron.theory.THEORY(truncate = truncate, interpolate = interpolate)
        conf['pp_hadron tabs'] = obslib.pp_hadron.reader.READER().load_data_sets('pp_hadron')
        conf['pp_hadron'].setup_interpolation()
        conf['pp_hadron grid'] = '%s/pp_hadron' % conf['grid path']
        conf['pp_hadron'].load_mtab_interp()
        self.pp_hadron_res = obslib.pp_hadron.residuals.RESIDUALS()

    def setup_sia(self):
        if self.sia_version==0:
            conf['sia tabs']= obslib.sia_V0.reader.READER().load_data_sets('sia')
            if 'ffpion'   in conf: conf['sia pion']   = obslib.sia_V0.theory.SIA('pion')
            if 'ffkaon'   in conf: conf['sia kaon']   = obslib.sia_V0.theory.SIA('kaon')
            if 'ffhadron' in conf: conf['sia hadron'] = obslib.sia_V0.theory.SIA('hadron')
            self.sia_res    = obslib.sia_V0.residuals.RESIDUALS()
        if self.sia_version==1:
            conf['sia tabs']= obslib.sia.reader.READER().load_data_sets('sia')
            conf['sia']     = obslib.sia.theory.SIA()
            self.sia_res    = obslib.sia.residuals.RESIDUALS()

    def setup_dy_pion(self):
        conf['dy-pion tabs']=obslib.dypion.reader.READER().load_data_sets('dy-pion')
        conf['dy-pion']=obslib.dypion.theory.DY_PION()
        conf['dy-pion'].load_melltab_hybrid()
        conf['dy-pion'].setup_parallel()
        if 'pion resummation' in conf and conf['pion resummation']:
            if conf['MP method']=='cosine' or conf['MP method']=='expansion':
                conf['resummed dy-pion']=obslib.dypion.dyres.DYRES()
                conf['resummed dy-pion'].setup_parallel()
            elif conf['MP method']=='double mellin':
                q2=[]
                exptsW=[]
                exptsPt=[]
                exptsC=[]
                for k in conf['dy-pion tabs']:
                    if k==11980:
                        exptsPt.append(k)
                        Qmin = np.unique(conf['dy-pion tabs'][k]['Qmin'])
                        Qmax = np.unique(conf['dy-pion tabs'][k]['Qmax'])
                        for i in range(len(Qmin)):
                            Qs = np.linspace(Qmin[i],Qmax[i],10)
                            for j in range(len(Qs)):
                                q2.append(Qs[j]**2)
                    elif k==20301:
                        exptsC.append(k)
                        Qmin = np.unique(conf['dy-pion tabs'][k]['Qmin'])
                        Qmax = np.unique(conf['dy-pion tabs'][k]['Qmax'])
                        for i in range(len(Qmin)):
                            Qs = np.linspace(Qmin[i],Qmax[i],10)
                            for j in range(len(Qs)):
                                q2.append(Qs[j]**2)
                    else:
                        exptsW.append(k)
                        for i in range(len(conf['dy-pion tabs'][k]['Q2'])):
                            q2.append(conf['dy-pion tabs'][k]['Q2'][i])
                conf['double mellin resum']=obslib.dypion.dyresmellin.DMELLIN(q2)
                if len(exptsW)!=0:
                    conf['pdf-W']=nucpdfaltdm.WPDF(exptsW)
                #else:
                #    conf['pdf-W']=nucpdfaltdm.WPDF()
                if len(exptsPt)!=0:
                    conf['pdf-Pt']=nucpdfaltdm.PTPDF(exptsPt)
                if len(exptsC)!=0:
                    conf['pdf-C']=nucpdfaltdm.CPDF(exptsC)
                #else:
                #    conf['pdf-Pt']=nucpdfaltdm.PTPDF()
                conf['resummed dy-pion']=obslib.dypion.dyres.DYRESDM()
                conf['resummed dy-pion'].setup_parallel()
        self.dy_pion_res=obslib.dypion.residuals.RESIDUALS()

    def setup_ln(self):
        conf['ln tabs']=obslib.LeadingBaryon.reader.READER().load_data_sets('ln')
        conf['pion-stfuncs']=obslib.LeadingBaryon.theory.STFUNCS()
        conf['ln']=obslib.LeadingBaryon.theory.LN()
        if 'idis stfuncs' not in conf:
            conf['path2idistab']='%s/grids/grids-idis/distab'%(os.environ['FITPACK'])
            if self.dis_version==0:
                conf['idis stfuncs']=obslib.idis_V0.theory.THEORY()
            else:
                conf['idis stfuncs']=obslib.idis.theory.THEORY()
        conf['ln'].setup_parallel()
        self.ln_res=obslib.LeadingBaryon.residuals.RESIDUALS()

    def setup_pion_qT(self):
        conf['pion_qT tabs']=obslib.DYpT.reader.READER().load_data_sets('pion_qT')
        #conf['pion_qT']=obslib.pion_qT.new_y_theory.PION_QT()
        conf['pion_qT']=obslib.DYpT.fo.FO()
        conf['pion_qT'].setup_parallel()
        self.pion_qTres=obslib.DYpT.residuals.RESIDUALS()

    def setup_DYpT(self): # This is done for fixed targe data
        conf['DYpT tabs']=obslib.DYpT.reader.READER().load_data_sets('DYpT')
        conf['DYpT']=obslib.DYpT.theory.THEORY()
        conf['DYpT'].setup_parallel()
        self.DYpT_res=obslib.DYpT.residuals.RESIDUALS()

    def setup_wc(self):

        self.wc_tabs = obslib.wc.reader.READER().load_data_sets('wc')
        conf['wc tabs'] = self.wc_tabs
        conf['path2wctab'] = '%s/wc' % conf['grid path']
        self.wc_thy  = obslib.wc.applgrid.APPLGRID()
        conf['wc'] = self.wc_thy
        self.wc_res = obslib.wc.residuals.RESIDUALS()
        if self.update_unpol==False: self.wc_thy._update()

    def setup_RpITD(self):
        conf['RpITD tabs']=obslib.RpITD.reader.READER().load_data_sets('RpITD')
        conf['RpITD']=obslib.RpITD.theory.RPITD()
        conf['RpITD'].setup_parallel()
        self.RpITD_res=obslib.RpITD.residuals.RESIDUALS()

    def setup_lqcd_mom(self):
        conf['lqcd_mom tabs']=obslib.lqcd_mom.reader.READER().load_data_sets('lqcd_mom')
        conf['lqcd_mom']=obslib.lqcd_mom.theory.MOMENTS()
        self.lqcd_mom_res=obslib.lqcd_mom.residuals.RESIDUALS()

    def setup_ccLCS(self):
        conf['ccLCS tabs']=obslib.ccLCS.reader.READER().load_data_sets('ccLCS')
        conf['ccLCS']=obslib.ccLCS.theory.CCLCS()
        conf['ccLCS'].setup_parallel()
        self.ccLCS_res=obslib.ccLCS.residuals.RESIDUALS()
        
    def setup_dy_qT(self): # This is done for collider data
        conf['dy_qT tabs']=obslib.dy_qT.reader.READER().load_data_sets('dy_qT')
        
        
        if conf['tmd settings']['method'] and  conf['tmd settings']['method'] == 'QZ':
            conf['dy_qT'] = obslib.dy_qT.theory.THEORY_QZ_TMD()
        #elif 'tmd method' in conf and conf['tmd method'] == 'ZETA':
        #    conf['dy_qT'] = obslib.dy_qT.theory.THEORY_ZETA() 
        #elif 'tmd method' in conf and conf['tmd method'] == 'CSS':
        #    conf['dy_qT'] = obslib.dy_qT.theory.THEORY_CSS() 
        elif conf['tmd settings']['method'] and  conf['tmd settings']['method'] == 'QZ_W':
            conf['dy_qT'] = obslib.dy_qT.theory.THEORY_QZ_W()
        elif conf['tmd settings']['method'] and  conf['tmd settings']['method'] == 'ZETA':
            conf['dy_qT'] = obslib.dy_qT.theory.THEORY_ZETA() 
        elif conf['tmd settings']['method'] and  conf['tmd settings']['method'] == 'CSS':
            if 'OPE' in conf['tmd settings']:
                conf['dy_qT'] = obslib.dy_qT.theory.THEORY_CSS(hadronA=conf['tmd settings']['OPE'],hadronB=conf['tmd settings']['OPE']) 
            else:
                conf['dy_qT'] = obslib.dy_qT.theory.THEORY_CSS() 
        elif conf['tmd settings']['method'] and  conf['tmd settings']['method'] == 'CSS1':
            conf['dy_qT'] = obslib.dy_qT.theory.THEORY_CSS1() 
            
        else:
            print("resman.py 571: Specify conf['tmd method']")
            sys.exit()
            
        self.dy_qT_res = obslib.dy_qT.residuals.RESIDUALS()
        conf['dy_qT'].setup_parallel()
        conf['dy_qT'].setup_parallel1() #--need to check if this is the best place for it.
        #if 'tmd method' in conf and conf['tmd method'] == 'QZ':
        if conf['tmd settings']['method'] and conf['tmd settings']['method'] == 'QZ':
            conf['dy_qT'].setup_OPEref()
            
    def setup_pp_BX(self):
        conf['pp_BX tabs']=obslib.pp_BX.reader.READER().load_data_sets('pp_BX')
        pionflag,kaonflag=False,False
        for k in conf['pp_BX tabs']:
            if conf['pp_BX tabs'][k]['reaction'][0]=='pp->nX': 
                pionflag = True
        if pionflag:
            conf['pp_nX'] = obslib.pp_BX.theory.PP2N_SPLITTING()
        self.pp_BX_res = obslib.pp_BX.residuals.RESIDUALS()

    #--parallelization

    def setup_parallel(self,nworkers):
        self.parallel=PARALLEL()
        self.parallel.task=self.task
        self.parallel.task1=self.task1
        self.parallel.set_state=self.set_state
        self.parallel.setup_master()
        self.parallel.setup_workers(nworkers)
        self.nworkers=nworkers

    def get_state(self,istate=1):
        state={}
        if istate==1:
            state['istate']=1
            if 'pdf'        in conf: state['pdf']        = conf['pdf'].get_state()
            if 'ppdf'       in conf: state['ppdf']       = conf['ppdf'].get_state()
            if 'ht4'        in conf: state['ht4']        = conf['ht4'].get_state()
            if 'off'        in conf: state['off']        = conf['off'].get_state()
            if 'ffpion'     in conf: state['ffpion']     = conf['ffpion'].get_state()
            if 'ffkaon'     in conf: state['ffkaon']     = conf['ffkaon'].get_state()
            if 'ffhadron'   in conf: state['ffhadron']   = conf['ffhadron'].get_state()
            if 'pdf-pion'   in conf: state['pdf-pion']   = conf['pdf-pion'].get_state()
            if 'pdf-kaon'   in conf: state['pdf-kaon']   = conf['pdf-kaon'].get_state()
            if 'ln'         in conf: state['ln']         = conf['ln'].get_state()
            #if 'tmd'        in conf: state['tmd']        = conf['tmd'].get_state()
            #if 'tmd-pion'   in conf: state['tmd-pion']   = conf['tmd-pion'].get_state()
            if 'g1T'        in conf: state['g1T']        = conf['g1T'].get_state()
            if 'RpITD'      in conf: state['RpITD']      = conf['RpITD'].get_state()
            if 'ccLCS'      in conf: state['ccLCS']      = conf['ccLCS'].get_state()
            if 'np tmd'     in conf: state['np tmd']     = conf['np tmd'].get_state()
            if 'np tmd universal' in conf: state['np tmd universal'] = conf['np tmd universal'].get_state()
            if 'dy_qT'      in conf: state['dy_qT']      = conf['dy_qT'].get_state()
        if istate==2:
            state['istate']=2
            if 'dy_qT'      in conf: state['dy_qT']      = conf['dy_qT'].get_state1()
            
        return state

    def set_state(self,state):
        if state['istate']==1:
            if 'pdf'        in conf: conf['pdf'].set_state(state['pdf'])
            if 'ppdf'       in conf: conf['ppdf'].set_state(state['ppdf'])
            if 'ht4'        in conf: conf['ht4'].set_state(state['ht4'])
            if 'off'        in conf: conf['off'].set_state(state['off'])
            if 'ffpion'     in conf: conf['ffpion'].set_state(state['ffpion'])
            if 'ffkaon'     in conf: conf['ffkaon'].set_state(state['ffkaon'])
            if 'ffhadron'   in conf: conf['ffhadron'].set_state(state['ffhadron'])
            if 'pdf-pion'   in conf: conf['pdf-pion'].set_state(state['pdf-pion'])
            if 'pdf-kaon'   in conf: conf['pdf-kaon'].set_state(state['pdf-kaon'])
            if 'ln'         in conf: conf['ln'].set_state(state['ln'])
            #if 'tmd'        in conf: conf['tmd'].set_state(state['tmd'])
            #if 'tmd-pion'   in conf: conf['tmd-pion'].set_state(state['tmd-pion'])
            if 'g1T'        in conf: conf['g1T'].set_state()
            if 'RpITD'      in conf: conf['RpITD'].set_state(state['RpITD'])
            if 'ccLCS'      in conf: conf['ccLCS'].set_state(state['ccLCS'])
            if 'np tmd'     in conf: conf['np tmd'].set_state(state['np tmd'])
            if 'np tmd universal' in conf: conf['np tmd universal'].set_state(state['np tmd universal'])
            if 'dy_qT'      in conf: conf['dy_qT'].set_state(state['dy_qT'])
        if state['istate']==2:
            if 'dy_qT'      in conf: conf['dy_qT'].set_state1(state['dy_qT'])

    def task(self,task):
        
        if task['reaction']=='dy':
            return self.dy_thy.process_request(task)

        if task['reaction']=='zrap':
            return self.zrap_thy.process_request(task)

        if task['reaction']=='wasym':
            return self.wasym_thy.process_request(task)

        if task['reaction']=='wzrv':
            return self.wzrv_thy.process_request(task,self.update_unpol)

        if task['reaction'] == 'jet':
            return conf['jet'].process_request(task)

        if task['reaction'] == 'pjet':
            return conf['pjet'].process_request(task)

        if task['reaction'] == 'pp_hadron':
            return conf['pp_hadron'].process_request(task)

        if task['reaction'] == 'dy-pion':
            return conf['dy-pion'].process_request(task)
        
        if task['reaction'] == 'dy-pion-res':
            return conf['resummed dy-pion'].process_request(task)

        if task['reaction'] == 'ln':
            return conf['ln'].process_request(task)

        if task['reaction'] == 'pion_qT':
            return conf['pion_qT'].process_request(task)

        if task['reaction'] == 'DYpT':
            return conf['DYpT'].process_request(task)

        if task['reaction'] == 'RpITD':
            return conf['RpITD'].process_request(task)

        if task['reaction'] == 'ccLCS':
            return conf['ccLCS'].process_request(task)
        
        if task['reaction'] == 'dy_qT':
            return conf['dy_qT'].process_request(task)
        
    def task1(self,task):
        
        if task['reaction'] == 'dy_qT':
            return conf['dy_qT'].process_request1(task)

    def get_residuals(self,par, initial = False):

        self.parman.set_new_params(par, initial = initial)

        data = conf['datasets']
        #if 'dy_qT' in data:
        #    conf['dy_qT'].setup_OPEref()

        ###################
        #--parallelization
        ###################
        #--idis:  not in parallel
        #--pidis: not in parallel
        #--SU23:  not in parallel
        #--sidis: not in parallel
        #--sia:   not in parallel
        #--dy:      in parallel
        #--zrap:    in parallel
        #--wasym:   in parallel
        #--wzrv:    in parallel
        #--jet:     in parallel
        #--pjet:    in parallel
        #--dy-pion: in parallel
        #--ln:      in parallel
        #--DYpT:    in parallel

        if self.parallel_mode:

            state=self.get_state(1)
            self.parallel.update_workers(state)

            #--Level 1
            tasks=[]
            if 'dy'          in  data:   tasks.extend(self.dy_thy.get_tasks())
            if 'zrap'        in  data:   tasks.extend(self.zrap_thy.get_tasks())
            if 'wasym'       in  data:   tasks.extend(self.wasym_thy.get_tasks())
            if 'wzrv'        in  data:   tasks.extend(self.wzrv_thy.get_tasks())
            if 'jet'         in  data:   tasks.extend(conf['jet'].tasks)
            if 'pjet'        in  data:   tasks.extend(conf['pjet'].tasks)
            if 'pp_hadron'   in  data:   tasks.extend(conf['pp_hadron'].tasks)
            if 'dy-pion'     in  data:   
                tasks.extend(conf['dy-pion'].get_tasks())
                if 'pion resummation' in conf and conf['pion resummation']: tasks.extend(conf['resummed dy-pion'].get_tasks())
            if 'ln'          in  data:   tasks.extend(conf['ln'].get_tasks())
            if 'pion_qT'     in  data:   tasks.extend(conf['pion_qT'].get_tasks())
            if 'DYpT'        in  data:   tasks.extend(conf['DYpT'].get_tasks())
            if 'RpITD'       in  data:   tasks.extend(conf['RpITD'].get_tasks())
            if 'ccLCS'       in  data:   tasks.extend(conf['ccLCS'].get_tasks())
            if 'dy_qT'       in  data:   tasks.extend(conf['dy_qT'].get_tasks())
            results=self.parallel.send_tasks(tasks)

            for task in results:
                if task['reaction']=='dy'           : self.dy_thy.update(task)
                if task['reaction']=='zrap'         : self.zrap_thy.update(task)
                if task['reaction']=='wasym'        : self.wasym_thy.update(task)
                if task['reaction']=='wzrv'         : self.wzrv_thy.update(task)
                if task['reaction']=='jet'          : conf['jet'].update(task)
                if task['reaction']=='pjet'         : conf['pjet'].update(task)
                if task['reaction']=='pp_hadron'    : conf['pp_hadron'].update(task)
                if task['reaction']=='dy-pion'      : conf['dy-pion'].update(task)
                if task['reaction']=='dy-pion-res'  : conf['resummed dy-pion'].update(task)
                if task['reaction']=='ln'           : conf['ln'].update(task)
                if task['reaction']=='pion_qT'      : conf['pion_qT'].update(task)
                if task['reaction']=='DYpT'         : conf['DYpT'].update(task)
                if task['reaction']=='RpITD'        : conf['RpITD'].update(task)
                if task['reaction']=='ccLCS'        : conf['ccLCS'].update(task)
                if task['reaction']=='dy_qT'        : conf['dy_qT'].update(task)

            #--Level 2 parallelization
            if 'dy_qT' in data:
                state=self.get_state(2)
                self.parallel.update_workers(state)

                #conf['dy_qT'].reshape() #--this helps with bookkeeping after performing first parallel
                tasks1 = conf['dy_qT'].get_tasks1()
                results1=self.parallel.send_tasks(tasks1,itask=1)
                for task in results1:
                    conf['dy_qT'].update1(task)


        else:
            if 'dy'          in  data:
                self.dy_thy._update()
                for _ in self.dy_thy.tasks:
                    self.dy_thy.update(_)
            if 'zrap'        in  data:
                self.zrap_thy._update()
                for _ in self.zrap_thy.tasks:
                    self.zrap_thy.update(_)
            if 'wasym'       in  data:
                self.wasym_thy._update()
                for _ in self.wasym_thy.tasks:
                    self.wasym_thy.update(_)
            if 'wzrv'        in  data:
                self.wzrv_thy._update()
                for _ in self.wzrv_thy.tasks:
                    self.wzrv_thy.update(_)
            if 'jet'         in data:
                for _ in conf['jet'].tasks:
                    conf['jet'].process_request(_)
                    conf['jet'].update(_)
            if 'pjet'        in data:
                for _ in conf['pjet'].tasks:
                    conf['pjet'].process_request(_)
                    conf['pjet'].update(_)
            if 'pp_hadron'   in data:
                for _ in conf['pp_hadron'].tasks:
                    conf['pp_hadron'].process_request(_)
                    conf['pp_hadron'].update(_)

            if 'dy-pion'     in  data:
                for _ in conf['dy-pion'].tasks:
                    #print(_)
                    #tasks.extend(conf['dy-pion'].get_tasks())
                    conf['dy-pion'].process_request(_)
                    conf['dy-pion'].update(_)
                if 'pion resummation' in conf and conf['pion resummation']:
                    for __ in conf['resummed dy-pion'].tasks:
                        #print(__)
                        #tasks.extend(conf['resummed dy-pion'].get_tasks())
                        conf['resummed dy-pion'].process_request(__)
                        conf['resummed dy-pion'].update(__)
            if 'ln'          in  data:
                for _ in conf['ln'].tasks:
                    conf['ln'].process_request(_)
                    conf['ln'].update(_)
                    #tasks.extend(conf['ln'].get_tasks())

            if 'dy_qT' in data:
                for _ in conf['dy_qT'].tasks:
                    conf['dy_qT'].process_request(_)
                    conf['dy_qT'].update(_)

            #--Level 2 without parallelization
            if 'dy_qT' in data:
                state = conf['dy_qT'].get_state1()
                conf['dy_qT'].set_state1(state)
                for _ in conf['dy_qT'].tasks1:
                    conf['dy_qT'].process_request1(_)
                    conf['dy_qT'].update1(_)

        #--Level 2
        if 'jet' in data:
            for dataset in conf['jet'].data:
                for eta_bin in conf['jet'].data[dataset]:
                    conf['jet'].pt_interpolation(dataset, eta_bin)
        if 'pjet' in data:
            for dataset in conf['pjet'].data:
                for eta_bin in conf['pjet'].data[dataset]:
                    conf['pjet'].pt_interpolation(dataset, eta_bin)
        if 'pp_hadron' in data:
            for dataset in conf['pp_hadron'].data:
                conf['pp_hadron'].pt_interpolation(dataset)
                

        #####################
        #--compute residuals
        #####################
        res,rres,nres=[],[],[]
        if 'idis' in conf['datasets']:
            #--update upol. SFs only if idis data is included in fit
            if self.update_unpol:
                if self.dis_version==0: self.idis_thy._update()
                out=self.idis_res.get_residuals()
                res=np.append(res,out[0])
                rres=np.append(rres,out[1])
                nres=np.append(nres,out[2])
            #--skip updating upol. SFs
            else: pass
        if 'pidis' in conf['datasets']:
            if self.dis_version==0: self.pidis_thy._update()
            out=self.pidis_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'SU23' in conf['datasets']:
            out=self.SU23_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'sidis' in conf['datasets']:
            out=self.sidis_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'psidis' in conf['datasets']:
            out=self.psidis_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'dy' in conf['datasets']:
            out=self.dy_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'zrap' in conf['datasets']:
            out=self.zrap_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'wasym' in conf['datasets']:
            out=self.wasym_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'wzrv' in conf['datasets']:
            out=self.wzrv_res.get_residuals()
            res=np.append(res,out[0])
            rres=np.append(rres,out[1])
            nres=np.append(nres,out[2])
        if 'jet' in conf['datasets']:
            out = self.jet_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'pjet' in conf['datasets']:
            out = self.pjet_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'pp_hadron' in conf['datasets']:
            out = self.pp_hadron_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'sia' in conf['datasets']:
            out = self.sia_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'positivity constraints' in conf and conf['positivity constraints']\
        or 'positivity_constraints' in conf and conf['positivity_constraints']:
            violations = []
            if 'ppdf' in conf:
                violations_ppdf = conf['ppdf'].check_positivity()
                violations.append(violations_ppdf)
                res = np.append(res,violations_ppdf)
            if 'pdf-kaon' in conf:
                violations_kaon = conf['pdf-kaon'].check_positivity()
                violations.append(violations_kaon)
                res = np.append(res,violations_kaon)
            conf['positivity chi2'] = np.sum(np.array(violations)**2)
        if 'pdf positivity' in conf and conf['pdf positivity']\
        or 'pdf_positivity' in conf and conf['pdf_positivity']:
            violations = conf['pdf'].check_positivity()
            res = np.append(res,violations)
            conf['pdf positivity chi2'] = np.sum(np.array(violations)**2)
        if 'fix_gA' in conf and conf['fix_gA']\
        or 'fix gA' in conf and conf['fix gA']:
            violations = conf['ppdf-mom'].check_gA()
            res = np.append(res,violations)
            conf['gA chi2'] = np.sum((violations)**2)
        if 'dy-pion' in conf['datasets']:
            out = self.dy_pion_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'ln' in conf['datasets']:
            out = self.ln_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'pion_qT' in conf['datasets']:
            out = self.pion_qTres.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'lqcd_mom' in conf['datasets']:
            _res,exp,thy = self.lqcd_mom_res.get_residuals()
            res = np.append(res,_res)
            conf['lqcd_mom chi2']   = np.sum((_res)**2)
            conf['lqcd_mom npts']   = len(_res)
            conf['lqcd_mom exp']    = exp
            conf['lqcd_mom thy']    = thy
        if 'DYpT' in conf['datasets']:
            out = self.DYpT_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'wc' in conf['datasets']:
            self.wc_thy._update()
            out = self.wc_res.get_residuals()
            res = np.append(res, out[0])
            rres = np.append(rres, out[1])
            nres = np.append(nres, out[2])
        if 'RpITD' in conf['datasets']:
            out = self.RpITD_res.get_RpITD_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'ccLCS' in conf['datasets']:
            out = self.ccLCS_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'dy_qT' in conf['datasets']:
            out = self.dy_qT_res.get_residuals()
            res = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'pp_BX' in conf['datasets']:
            out  = self.pp_BX_res.get_residuals()
            res  = np.append(res,out[0])
            rres = np.append(rres,out[1])
            nres = np.append(nres,out[2])
        if 'nucpdf positivity' in conf and conf['nucpdf positivity']:
            violations = []
            violations_nucpdf = conf['idis'].off_model.check_nucpdf_positivity()
            violations.append(violations_nucpdf)
            res = np.append(res,violations_nucpdf)
            conf['nucpdf positivity chi2'] = np.sum(np.array(violations)**2)

        if self.lqcd_itd:
            _res,exp,thy = self.lqcd_itd_res.get_residuals()
            res = np.append(res,_res)
            conf['lqcd_itd chi2']   = np.sum((_res)**2)
            conf['lqcd_itd npts']   = len(_res)
            conf['lqcd_itd exp']    = exp
            conf['lqcd_itd thy']    = thy

        return res,rres,nres

    #--aux funcs

    def gen_report(self,verb=0,level=0):
        lines = []
        if ('idis' in conf['datasets']) and (len(conf['datasets']['idis']['xlsx']) != 0):
            lines.extend(self.idis_res._gen_report(verb,level))
        if 'pidis'     in conf['datasets']: lines.extend(self.pidis_res.gen_report(verb,level))
        if 'SU23'      in conf['datasets']: lines.extend(self.SU23_res.gen_report(verb,level))
        if 'sidis'     in conf['datasets']: lines.extend(self.sidis_res.gen_report(verb,level))
        if 'psidis'    in conf['datasets']: lines.extend(self.psidis_res.gen_report(verb,level))
        if 'dy'        in conf['datasets']: lines.extend(self.dy_res.gen_report(verb,level))
        if 'zrap'      in conf['datasets']: lines.extend(self.zrap_res.gen_report(verb,level))
        if 'wasym'     in conf['datasets']: lines.extend(self.wasym_res.gen_report(verb,level))
        if 'wzrv'      in conf['datasets']: lines.extend(self.wzrv_res.gen_report(verb,level))
        if 'jet'       in conf['datasets']: lines.extend(self.jet_res.gen_report(verb, level))
        if 'pjet'      in conf['datasets']: lines.extend(self.pjet_res.gen_report(verb, level))
        if 'pp_hadron' in conf['datasets']: lines.extend(self.pp_hadron_res.gen_report(verb, level))
        if 'sia'       in conf['datasets']: lines.extend(self.sia_res.gen_report(verb, level))
        if 'dy-pion'   in conf['datasets']: lines.extend(self.dy_pion_res.gen_report(verb, level))
        if 'ln'        in conf['datasets']: lines.extend(self.ln_res.gen_report(verb, level))
        if 'DYpT'      in conf['datasets']: lines.extend(self.DYpT_res.gen_report(verb, level))
        if 'wc'        in conf['datasets']: lines.extend(self.wc_res.gen_report(verb,level))
        if 'RpITD'     in conf['datasets']: lines.extend(self.RpITD_res.gen_report(verb, level))
        if 'ccLCS'     in conf['datasets']: lines.extend(self.ccLCS_res.gen_report(verb, level))
        if 'lqcd_mom'  in conf['datasets']: lines.extend(self.lqcd_mom_res.gen_report(verb, level))
        if 'dy_qT'     in conf['datasets']: lines.extend(self.dy_qT_res.gen_report(verb, level))
        if 'pp_BX'     in conf['datasets']: lines.extend(self.pp_BX_res.gen_report(verb, level))

        if 'positivity constraints' in conf and conf['positivity constraints']\
        or 'positivity_constraints' in conf and conf['positivity_constraints']:
            if 'ppdf' in conf: lines.extend(conf['ppdf'].gen_report(verb, level))
            if 'pdf-kaon' in conf: lines.extend(conf['pdf-kaon'].gen_report(verb,level))

        if 'pdf positivity' in conf and conf['pdf positivity']\
        or 'pdf_positivity' in conf and conf['pdf_positivity']:
            lines.extend(conf['pdf'].gen_report(verb, level))

        if 'fix_gA' in conf and conf['fix_gA']\
        or 'fix gA' in conf and conf['fix gA']:
            lines.extend(conf['ppdf-mom'].gen_report(verb,level))

        if self.lqcd_itd: lines.extend(self.lqcd_itd_res.gen_report())

        if 'nucpdf positivity' in conf and conf['nucpdf positivity']:
            lines.extend(conf['idis'].off_model.gen_report(verb, level))

        return lines

    #--don't use this one!
    def _gen_report(self,verb=1,level=0):
        data={}
        if 'idis'      in conf['datasets']: data['idis']=self.idis_res.gen_report(verb,level)
        if 'dy'        in conf['datasets']: data['dy']=self.dy_res.gen_report(verb,level)
        if 'pidis'     in conf['datasets']: data['pidis']=self.pidis_res.gen_report(verb,level)
        if 'SU23'      in conf['datasets']: data['SU23']=self.SU23_res.gen_report(verb,level)
        if 'sidis'     in conf['datasets']: data['sidis']=self.sidis_res.gen_report(verb,level)
        if 'psidis'    in conf['datasets']: data['psidis']=self.psidis_res.gen_report(verb,level)
        if 'dy'        in conf['datasets']: data['dy']=self.dy_res.gen_report(verb,level)
        if 'zrap'      in conf['datasets']: data['zrap']=self.zrap_res.gen_report(verb,level)
        if 'wasym'     in conf['datasets']: data['wasym']=self.wasym_res.gen_report(verb,level)
        if 'wzrv'      in conf['datasets']: data['wzrv']=self.wzrv_res.gen_report(verb,level)
        if 'jet'       in conf['datasets']: data['jet']=self.jet_res.gen_report(verb, level)
        if 'pjet'      in conf['datasets']: data['pjet']=self.pjet_res.gen_report(verb, level)
        if 'pp_hadron' in conf['datasets']: data['pp_hadron']=self.pp_hadron_res.gen_report(verb, level)
        if 'sia'       in conf['datasets']: data['sia']=self.sia_res.gen_report(verb, level)
        if 'dy-pion'   in conf['datasets']: data['dy-pion']=self.dy_pion_res.gen_report(verb, level)
        if 'ln'        in conf['datasets']: data['ln']=self.ln_res.gen_report(verb, level)
        if 'DYpT'      in conf['datasets']: data['DYpT']=self.DYpT_res.gen_report(verb, level)
        if 'wc'        in conf['datasets']: data['wc']=self.wc_res.gen_report(verb,level)
        if 'RpITD'     in conf['datasets']: data['RpITD']=self.RpITD_res.gen_report(verb, level)
        if 'ccLCS'     in conf['datasets']: data['ccLCS']=self.ccLCS_res.gen_report(verb, level)
        if 'dy_qT'     in conf['datasets']: data['dy_qT']=self.dy_qT_res.gen_report(verb, level)
        return data

    def get_chi2(self):
        data={}
        if 'idis'          in conf['datasets']: data.update(self.idis_res.get_chi2())
        if 'pidis'         in conf['datasets']: data.update(self.pidis_res.get_chi2())
        if 'SU23'          in conf['datasets']: data.update(self.SU23_res.get_chi2())
        if 'sidis'         in conf['datasets']: data.update(self.sidis_res.get_chi2())
        if 'psidis'        in conf['datasets']: data.update(self.psidis_res.get_chi2())
        if 'dy'            in conf['datasets']: data.update(self.dy_res.get_chi2())
        if 'zrap'          in conf['datasets']: data.update(self.zrap_res.get_chi2())
        if 'wasym'         in conf['datasets']: data.update(self.wasym_res.get_chi2())
        if 'wzrv'          in conf['datasets']: data.update(self.wzrv_res.get_chi2())
        if 'jet'           in conf['datasets']: data.update(self.jet_res.get_chi2())
        if 'pjet'          in conf['datasets']: data.update(self.pjet_res.get_chi2())
        if 'pp_hadron'     in conf['datasets']: data.update(self.pp_hadron_res.get_chi2())
        if 'sia'           in conf['datasets']: data.update(self.sia_res.get_chi2())
        if 'dy-pion'       in conf['datasets']: data.update(self.dy_pion_res.get_chi2())
        if 'ln'            in conf['datasets']: data.update(self.ln_res.get_chi2())
        if 'DYpT'          in conf['datasets']: data.update(self.DYpT_res.get_chi2())
        if 'wc'            in conf['datasets']: data.update(self.wc_res.get_chi2())
        if 'RpITD'         in conf['datasets']: data.update(self.RpITD_res.get_chi2())
        if 'ccLCS'         in conf['datasets']: data.update(self.ccLCS_res.get_chi2())
        if 'dy_qT'         in conf['datasets']: data.update(self.dy_qT_res.get_chi2())
        return data

    def test(self,ntasks=1):
        #--loop over states
        import psutil
        t=time.time()
        for _ in range(ntasks):
            par=self.parman.gen_flat(setup=True)
            res,rres,nres=self.get_residuals(par)
            chi2=np.sum(res**2)
            chi2 /= float(len(res))
            print('(%03d/%03d) chi2=%f'%(_ + 1, ntasks, chi2))
        process = psutil.Process(os.getpid())
        memory_rss = process.memory_info().rss / (1024.0 ** 3.0)
        # memory_vms = process.memory_info().vms / (1024.0 ** 3.0) ## memory allocated, including swapped memory
        elapsed_time=time.time()-t
        # print('with %d workers, elapsed time: %f' % (self.nworkers, elapsed_time))
        return elapsed_time, memory_rss

    def shutdown(self):
        if self.parallel_mode:
            self.parallel.stop_workers()






