# -----------------------------------------------------------------------------
#   Copyright (C): MedAustron GmbH, ACMIT Gmbh and Medical University Vienna
#   This software is distributed under the terms
#   of the GNU Lesser General  Public Licence (LGPL)
#   See LICENSE for further details
# -----------------------------------------------------------------------------

"""
This module implements the job executor, which uses a "system configuration"
(system configuration object) and a "plan details" objbect to prepare the
simulation subjobs, to run them, and to combine the partial dose distributions
into a final DICOM dose and to report success or failure.

The job executor is normally created *after* a 'idc_details' object has been
created and configured with the all the plan details and user wishes. The job
executor then creates the Gate workspace and cluster submit files. The job is
not immediately submitted to the cluster: if the user interacts with the
"socrates" GUI, then the user can inspect the configuration by running a
limited number of primaries and visualizing the result with "Gate --qt". After
the OK by the user, the job is then submitted to the cluster and the control is
taken over by the "job control daemon", which monitors the number of simulated
primaries, the statistical uncertainty and the elapsed time since the start of
the simulation and decides when to stop the simulations and accumulate the
final results.
"""

################################################################################
# API
################################################################################

class job_executor(object):
    @staticmethod
    def create_condor_job_executor(details):
        return condor_job_executor(details)
    @property
    def template_gate_work_directory(self):
        return self._RUNGATE_submit_directory
    @property
    def summary(self):
        return self._summary
    @property
    def details(self):
        return self._details
    @property
    def estimated_calculation_time(self):
        return self._ect
    def launch_gate_qt_check(self,beamname):
        return self._launch_gate_qt_check(beamname)
    def launch_subjobs(self):
        return self._launch_subjobs()

################################################################################
# IMPLEMENTATION
################################################################################

# python standard library imports
import os, stat
import re
import logging
import shutil
import time
import tarfile
from glob import glob
from subprocess import Popen
import numpy as np
import itk

# IDEAL imports
from utils.gate_pbs_plan_file import gate_pbs_plan_file
from impl.beamline_model import beamline_model
from impl.gate_macro import write_gate_macro_file
from impl.hlut_conf import hlut_conf
from impl.idc_enum_types import MCStatType
from impl.system_configuration import system_configuration

logger = logging.getLogger(__name__)

class condor_job_executor(job_executor):
    def __init__(self,details):
        syscfg = system_configuration.getInstance()
        self._details = details
        self._ect = 0.
        self._badchars=re.compile("[^a-zA-Z0-9_]")
        self._time_stamp = time.strftime("%Y_%m_%d_%H_%M_%S")
        self._summary=str()
        self._summary+="TPS Plan/Beamset UID: " + details.uid + "\n"
        self._summary+="IDC user: {}\n".format(syscfg['username'])
        self._set_cleanup_policy(not syscfg['debug'])
        self._mac_files=[]
        self._qspecs={}
        self._generate_RUNGATE_submit_directory()
        self._populate_RUNGATE_submit_directory()
    def _set_cleanup_policy(self,cleanup):
        self._cleanup = cleanup
        if cleanup:
            logger.debug("cleanup requested (no debugging): WILL DELETE intermediate simulation results, keep only final results")
        else:
            logger.debug("debugging requested (no cleaning): intermediate simulation results WILL NOT BE DELETED")
    def _generate_RUNGATE_submit_directory(self):
        plan_uid = self.details.uid
        user = self.details.username
        runid = 0
        while True:
            rungate_dir = os.path.join(self.details.tmpdir_job,f"rungate.{runid}")
            if os.path.exists(rungate_dir):
                runid += 1
            else:
                break
        logger.debug("RUNGATE submit directory is {}".format(rungate_dir))
        os.mkdir(rungate_dir)
        logger.debug("created template subjob work directory {}".format(rungate_dir))
        self._RUNGATE_submit_directory = rungate_dir
    def _get_ncores(self):
        # TODO: make Ncores (number of subjobs for current calculation) flexible:
        # * depending on urgency/priority
        # * depending on how busy the cluster is
        # * avoid that calculation time will depend on a single job that takes forever
        syscfg = system_configuration.getInstance()
        return syscfg['number of cores']
    def _populate_RUNGATE_submit_directory(self):
        """
        Create the content of the Gate directory: how to run the simulation.
        TODO: This method implementation is very long, should be broken up in smaller entities.
        TODO: In particular, the HTCondor-specific stuff should be separated out,
              so that it will be easier to also support other cluster job management systems, like SLURM and OpenPBS.
        """
        ####################
        syscfg = system_configuration.getInstance()
        ####################
        save_cwd = os.getcwd()
        assert( os.path.isdir( self._RUNGATE_submit_directory ) )
        os.chdir( self._RUNGATE_submit_directory )
        for d in ["output","mac","data","logs"]:
            os.mkdir(d)
        ####################
        shutil.copy(os.path.join(syscfg['commissioning'], syscfg['materials database']),
                    os.path.join("data",syscfg['materials database']))
        ct=self.details.run_with_CT_geometry
        ct_bb=None
        if ct:
            #shutil.copytree(syscfg["CT"],os.path.join("data","CT"))
            dataCT = os.path.join(os.path.realpath("./data"),"CT")
            os.mkdir(dataCT)
            shutil.copy(os.path.join(syscfg["CT"],"ct-parameters.mac"),os.path.join(dataCT,"ct-parameters.mac"))
            #### HUtol=str(syscfg['hu density tolerance [g/cm3]'])
            #### hlutdensity=os.path.realpath(self.details.HLUTdensity)
            #### hlutmaterials=os.path.realpath(self.details.HLUTmaterials)
            #### # the name of the cache directory contains the MD5 sum of the contents of the density & material files as well as the HUtol value
            #### cache_dir = hlut_cache_dir(density     = hlutdensity,
            ####                            composition = hlutmaterials,
            ####                            HUtol       = HUtol,
            ####                            create      = False) # returns None if cache directory does not exist
            #### if cache_dir is None:
            ####     # generate cache
            ####     # TODO: IDC also does this (in UpdateHURange), should we really do this also here?
            ####     logger.info(f"going to generate missing material cache dir {cache_dir}")
            ####     success, cache_dir = generate_hlut_cache(hlutdensity,hlutmaterials,HUtol)
            ####     if not success:
            ####         raise RuntimeError("failed to create material cache for HLUT={hlutdensity} and composition={hlutmaterials}")
            #### cached_hu2mat=os.path.join(cache_dir,"patient-HU2mat.txt")
            #### cached_humdb=os.path.join(cache_dir,"patient-HUmaterials.db")
            all_hluts = hlut_conf.getInstance()
            # TODO: should 'idc_details' ask the user about a HU density tolerance value?
            # TODO: should we try to catch the exceptions that 'all_hluts' might throw at us?
            cached_hu2mat_txt, cached_humat_db = all_hluts[self.details.ctprotocol_name].get_hu2mat_files()
            hudensity = all_hluts[self.details.ctprotocol_name].get_density_file()
            hu2mat_txt=os.path.join(dataCT,os.path.basename(cached_hu2mat_txt))
            humat_db=os.path.join(dataCT,os.path.basename(cached_humat_db))
            shutil.copy(cached_hu2mat_txt,hu2mat_txt)
            shutil.copy(cached_humat_db,humat_db)
            mcpatientCT_filepath = os.path.join(dataCT,self.details.uid.replace(".","_")+".mhd")
            ct_bb,ct_nvoxels=self.details.WritePreProcessingConfigFile(self._RUNGATE_submit_directory,mcpatientCT_filepath,hu2mat_txt,hudensity)
            msg = "IDC with CT geometry"
        else:
            # TODO: should we try to only copy the relevant phantom data, instead of the entire phantom collection?
            shutil.copytree(syscfg["phantoms"],os.path.join("data","phantoms"))
            msg = "IDC with PHANTOM geometry"
        logger.debug(msg)
        self._summary += msg+'\n'
        ####################
        beamset = self.details.bs_info
        beamsetname = re.sub(self._badchars,"_",beamset.name)
        if ct:
            # the name has to end in PLAN
            plan_dose_file = f"idc-CT-{beamsetname}-PLAN"
        else:
            phantom_name=self.details.PhantomSpecs.label
            plan_dose_file = f"idc-PHANTOM-{phantom_name}-{beamsetname}-PLAN"
        spotfile = os.path.join("data","TreatmentPlan4Gate-{}.txt".format(beamset.name.replace(" ","_")))
        gate_plan = gate_pbs_plan_file(spotfile,allow0=True)
        gate_plan.import_from(beamset)
        ncores = self._get_ncores()
        beamlines=list()
        for beam in beamset.beams:
            logger.debug(f"configuring beam {beam.Name}")
            bmlname = beam.TreatmentMachineName if self.details.beamline_override is None else self.details.beamline_override
            logger.debug(f"beamline name is {bmlname}")
            beamnr = beam.Number
            beamname = re.sub(self._badchars,"_",beam.Name)
            if beamname == beam.Name:
                self._summary += "beam: '{}'\n".format(beamname)
            else:
                self._summary += "beam: '{}'/'{}'\n".format(beam.Name,beamname)
            radtype = beam.RadiationType
            if radtype.upper() == 'PROTON':
                physlist=syscfg['proton physics list']
            elif radtype.upper()[:3] == 'ION':
                physlist=syscfg['ion physics list']
            else:
                raise RuntimeError("don't know which physics list to use for radiation type '{}'".format(radtype))
            if not self.details.BeamIsSelected(beam.Name):
                msg = "skip simulation for de-selected beam name={} nr={} machine={}.".format(beamname,beamnr,bmlname)
                logger.warn(msg)
                continue
            # TODO: do we need this distinction between ncores and njobs?
            # maybe we'll need this for when the uncertainty goal needs to apply to the plan dose instead of beam dose?
            njobs = ncores
            if self.details.run_with_CT_geometry:
                rsids = beam.RangeShifterIDs
                rmids = beam.RangeModulatorIDs
            else:
                rsids = self.details.RSOverrides.get(beam.Name,beam.RangeShifterIDs)
                rmids = self.details.RMOverrides.get(beam.Name,beam.RangeModulatorIDs)
            rsflag="(as PLANNED)" if rsids == beam.RangeShifterIDs else "(OVERRIDE)"
            rmflag="(as PLANNED)" if rmids == beam.RangeModulatorIDs else "(OVERRIDE)"
            bml = beamline_model.get_beamline_model_data(bmlname, syscfg['beamlines'])
            if not bml.has_radtype(radtype):
                msg = "BeamNumber={}, BeamName={}, BeamLine={}\n".format(beamnr,beamname,bmlname)
                msg += "* ERROR: simulation not possible: no source props file for radiation type {}\n".format(radtype)
                logger.warn(msg)
                self._summary += msg
                continue
            msg  = "BeamNumber={}, BeamName={}, BeamLine={}\n".format(beamnr,beamname,bmlname)
            msg += "* Range shifter(s): {} {}\n".format( ("NONE" if len(rsids)==0 else ",".join(rsids)),rsflag)
            msg += "* Range modulator(s): {} {}\n".format( ("NONE" if len(rmids)==0 else ",".join(rmids)),rmflag)
            #msg += "* {} primaries per job => est. {} s/job.\n".format(nprim,dt)
            logger.debug(msg)
            if rsflag == "(OVERRIDE)" or rmflag == "(OVERRIDE)":
                self._summary += msg
            if self.details.dosegrid_changed:
                self._summary += "dose grid resolution changed to {}\n".format(self.details.GetNVoxels())
            #TODO: change api for 'write_gate_macro_file' to take fewer arguments
            macfile_input = dict( beamset=beamsetname,
                                  uid=self.details.uid,
                                  spotfile=spotfile,
                                  beamline=bml,
                                  beamnr=beamnr,
                                  beamname=beamname,
                                  radtype=radtype,
                                  rsids=rsids,
                                  rmids=rmids,
                                  physicslist=physlist)
            if ct:
                nominal_patient_angle = beam.patient_angle
                mod_patient_angle = (360.0 - beam.patient_angle) % 360.0
                macfile_input.update( ct=True,
                                      ct_mhd=mcpatientCT_filepath,
                                      dose_center =self.details.GetDoseCenter(),
                                      dose_size =self.details.GetDoseSize(),
                                      ct_bb = ct_bb,
                                      dose_nvoxels=ct_nvoxels,
                                      mod_patient_angle=mod_patient_angle,
                                      gantry_angle=beam.gantry_angle,
                                      isoC=np.array(beam.IsoCenter),
                                      HU2mat=hu2mat_txt,
                                      HUmaterials=humat_db )
            else:
                macfile_input.update( ct=False,
                                      dose_nvoxels=self.details.GetNVoxels(),
                                      isoC=np.array(self.details.PhantomISOinMM(beam.Name)),
                                      phantom=self.details.PhantomSpecs )
                # the following two lines are not strictly necessary
                phpath = self.details.PhantomSpecs.mac_file_path
                shutil.copy(phpath,os.path.join("data","phantoms",os.path.basename(phpath)))
            main_macfile,beam_dose_mhd = write_gate_macro_file( **macfile_input )
            assert(main_macfile not in self._mac_files) # should never happen
            self._mac_files.append(main_macfile)
            #
            def_dose_corr_factor=syscfg['(tmp) correction factors']["default"]
            dose_corr_key=(bmlname+"_"+radtype).lower()
            dose_corr_factor=syscfg['(tmp) correction factors'].get(dose_corr_key,def_dose_corr_factor)
            #
            self._qspecs[beamname]=dict(nJobs=str(njobs),
                                        #nMC=str(nprim),
                                        #nMCtot=str(nprimtot),
                                        origname=beam.Name,
                                        dosecorrfactor=str(dose_corr_factor),
                                        dosemhd=beam_dose_mhd,
                                        macfile=main_macfile,
                                        dose2water=str(ct or self.details.PhantomSpecs.dose_to_water),
                                        isocenter=" ".join(["{}".format(v) for v in beam.IsoCenter]))
            shutil.copy(bml.source_properties_file(radtype),"data")
            for rs in rsids:
                dest=os.path.join("mac",os.path.basename(bml.rs_details_mac_file(rs)))
                if not os.path.exists(dest):
                    shutil.copy(bml.rs_details_mac_file(rs),dest)
            for rm in rmids:
                dest=os.path.join("mac",os.path.basename(bml.rm_details_mac_file(rm)))
                if not os.path.exists(dest):
                    shutil.copy(bml.rm_details_mac_file(rm),dest)
            if bmlname in beamlines:
                continue
            if bml.beamline_details_mac_file:
                shutil.copy(bml.beamline_details_mac_file,"mac")
                for a in bml.beamline_details_aux:
                    dest=os.path.join("data",os.path.basename(a))
                    if os.path.exists(dest):
                        raise RuntimeError("CONFIG ERROR")
                    if os.path.isdir(a):
                        shutil.copytree(a,dest)
                    else:
                        shutil.copy(a,dest)
                for a in bml.common_aux:
                    dest=os.path.join("data",os.path.basename(a))
                    if os.path.exists(dest):
                        continue
                    if os.path.isdir(a):
                        shutil.copytree(a,dest)
                    else:
                        shutil.copy(a,dest)
            beamlines.append(bmlname)
        logger.debug("copied all beam line models into data directory")
        logger.debug("wrote mac files for all beams to be simulated")
        #self._summary + "{} seconds estimated for simulation of whole plan".format(self._ect)
        rsd=self._RUNGATE_submit_directory
        os.makedirs(os.path.join(rsd,"tmp"),exist_ok=True) # the 'mode' argument is ignored (not only on Windows)
        os.chmod(os.path.join(rsd,"tmp"),mode=0o777)
        with open("RunGATE.sh","w") as jobsh:
            jobsh.write("#!/bin/bash\n")
            jobsh.write("set -x\n")
            jobsh.write("set -e\n")
            jobsh.write("whoami\n")
            jobsh.write("who am i\n")
            jobsh.write("date\n")
            jobsh.write("echo $# arguments\n")
            jobsh.write('echo "pwd=$(pwd)"\n')
            jobsh.write("macfile=$1\n")
            jobsh.write("export clusterid=$2\n")
            jobsh.write("export procid=$3\n")
            jobsh.write("pwd -P\n")
            jobsh.write("pwd -L\n")
            jobsh.write(f"cd {rsd}\n")
            jobsh.write("pwd -P\n")
            jobsh.write("pwd -L\n")
            #jobsh.write("tar zxvf macdata.tar.gz\n")
            #if ct:
            #    #jobsh.write("cat data/HUoverrides.txt >> data/patient-HU2mat.txt\n")
            #    jobsh.write("tar zxvf ct.tar.gz\n")
            jobsh.write("outputdir=./output.$clusterid.$procid\n")
            jobsh.write("tmpoutputdir=./tmp/output.$clusterid.$procid\n")
            jobsh.write("mkdir $outputdir\n")
            jobsh.write("mkdir $tmpoutputdir\n")
            jobsh.write("chmod 777 ./tmp/$outputdir\n")
            #jobsh.write("mkdir {}/$outputdir\n".format(rsd))
            #jobsh.write('touch "$outputdir/START_$(basename $macfile)"\n')
            #jobsh.write("ln -s {}/$outputdir\n".format(rsd))
            #jobsh.write("ln -s {}/mac\n".format(rsd))
            #jobsh.write("ln -s {}/data\n".format(rsd))
            jobsh.write("source {}\n".format(os.path.join(syscfg['bindir'],"IDEAL_env.sh")))
            jobsh.write("source {}\n".format(syscfg['gate_env.sh']))
            jobsh.write("seed=$[1000*clusterid+procid]\n")
            jobsh.write("echo rng seed is $seed\n")
            jobsh.write("ret=0\n")
            # with the following construction, Gate can crash without DAGman removing all remaining jobs in the queue
            jobsh.write("Gate -a[RNGSEED,$seed][RUNMAC,mac/run_all.mac][VISUMAC,mac/novisu.mac][OUTPUTDIR,$outputdir] $macfile && echo GATE SUCCEEDED || ret=$? \n")
            jobsh.write("if [ $ret -ne 0 ] ; then echo GATE FAILED WITH EXIT CODE $ret; fi\n")
            # the following is used both in postprocessing and by the job_control_daemon
            jobsh.write("echo $ret > $outputdir/gate_exit_value.txt\n")
            jobsh.write("du -hcs *\n")
            jobsh.write("date\n")
            jobsh.write("echo SECONDS=$SECONDS\n")
            jobsh.write("exit 0\n")
        os.chmod("RunGATE.sh",stat.S_IREAD|stat.S_IRWXU)
        logger.debug("wrote run shell script")
        with open("RunGATEqt.sh","w") as jobsh:
            jobsh.write("#!/bin/bash\n")
            jobsh.write("set -x\n")
            jobsh.write("set -e\n")
            jobsh.write("macfile=$1\n")
            jobsh.write("outputdir=output_qt\n")
            jobsh.write("rm -rf $outputdir\n")
            jobsh.write("mkdir -p $outputdir\n")
            jobsh.write("source {}\n".format(syscfg['gate_env.sh']))
            if ct:
                #jobsh.write("cat data/HUoverrides.txt >> data/patient-HU2mat.txt\n")
                jobsh.write("echo running preprocess, may take a minute or two...\n")
                jobsh.write("time {}/preprocess_ct_image.py\n".format(syscfg["bindir"]))
            jobsh.write("echo starting Gate, may take a minute...\n")
            jobsh.write("Gate --qt -a[RUNMAC,mac/run_qt.mac][VISUMAC,mac/visu.mac][OUTPUTDIR,$outputdir] $macfile\n")
            jobsh.write("du -hcs *\n")
        os.chmod("RunGATEqt.sh",stat.S_IREAD|stat.S_IRWXU)
        logger.debug("wrote run debugging shell script with GUI")
        # TODO: write the condor stuff directly in python?
        input_files = ["RunGATE.sh", "macdata.tar.gz","{}/locked_copy.py".format(syscfg["bindir"])]
        if ct:
            input_files.append("ct.tar.gz")
        with open("RunGATE.submit","w") as jobsubmit:
            jobsubmit.write("universe = vanilla\n")
            jobsubmit.write("executable = {}/RunGATE.sh\n".format(self._RUNGATE_submit_directory))
            jobsubmit.write("should_transfer_files = NO\n")
            jobsubmit.write(f'+workdir = "{self._RUNGATE_submit_directory}"\n')
            jobsubmit.write("priority = {}\n".format(self.details.priority))
            jobsubmit.write("request_cpus = 1\n")
            # cluster job diagnostics:
            jobsubmit.write("output = logs/stdout.$(CLUSTER).$(PROCESS).txt\n")
            jobsubmit.write("error = logs/stderr.$(CLUSTER).$(PROCESS).txt\n")
            jobsubmit.write("log = logs/stdlog.$(CLUSTER).$(PROCESS).txt\n")
            # boiler plate
            jobsubmit.write("RunAsOwner = true\n")
            jobsubmit.write("nice_user = false\n")
            jobsubmit.write("next_job_start_delay = {}\n".format(syscfg["htcondor next job start delay [s]"]))
            jobsubmit.write("notification = error\n")
            # the actual submit command:
            for beamname,qspec in self._qspecs.items():
                origname=qspec["origname"]
                jobsubmit.write("request_memory = {}\n".format(self.details.calculate_ram_request_mb(origname)))
                jobsubmit.write("arguments = {} $(CLUSTER) $(PROCESS)\n".format(qspec['macfile']))
                jobsubmit.write("queue {}\n".format(qspec['nJobs']))
        os.chmod("RunGATE.submit",stat.S_IREAD|stat.S_IWUSR)
        logger.debug("wrote condor submit file")
        self.details.WritePostProcessingConfigFile(self._RUNGATE_submit_directory,self._qspecs,plan_dose_file)
        with open("RunGATE.dagman","w") as dagman:
            if ct:
                dagman.write("SCRIPT PRE  rungate {}/preprocess_ct_image.py\n".format(syscfg["bindir"]))
            dagman.write("JOB         rungate ./RunGATE.submit\n")
            dagman.write("SCRIPT POST rungate {}/postprocess_dose_results.py\n".format(syscfg["bindir"]))
        os.chmod("RunGATE.dagman",stat.S_IREAD|stat.S_IWUSR)
        logger.debug("wrote condor dagman file")
        with tarfile.open("macdata.tar.gz","w:gz") as tar:
            tar.add("mac")
            tar.add("data")
        logger.debug("wrote gzipped tar file with 'data' and 'mac' directory")
        os.chdir( save_cwd )
    def _launch_gate_qt_check(self,beam_name):
        beamname = re.sub(self._badchars,"_",beam_name)
        if beamname not in self._qspecs.keys():
            logger.error("cannot run gate-QT viewer: unknown beam name '{}'".format(beamname))
            return -1
        macfile = self._qspecs[beamname]['macfile']
        if not os.path.isdir( self._RUNGATE_submit_directory ):
            logger.error("cannot find submit directory {}".format(self._RUNGATE_submit_directory))
            return -2
        save_cwd = os.getcwd()
        os.chdir( self._RUNGATE_submit_directory)
        ret=os.system( " ".join(["./RunGATEqt.sh",macfile]))
        logger.debug("RunGATEqt.sh exited with return code {}".format(ret))
        os.chdir( save_cwd )
        logger.debug("ret={} has type {}".format(ret,type(ret)))
        return ret
    def _launch_subjobs(self):
        if not os.path.isdir( self._RUNGATE_submit_directory ):
            logger.error("cannot find submit directory {}".format(self._RUNGATE_submit_directory))
            return -1
        syscfg = system_configuration.getInstance()
        save_cwd = os.getcwd()
        os.chdir( self._RUNGATE_submit_directory )
        ymd_hms = time.strftime("%Y-%m-%d %H:%M:%S")
        userstuff = self.details.WriteUserSettings(self._qspecs,ymd_hms,self._RUNGATE_submit_directory)
        ret=os.system( "condor_submit_dag ./RunGATE.dagman")
        if ret==0:
            msg = "Job submitted at {}.\n".format(ymd_hms)
            msg += "User settings are summarized in \n'{}'\n".format(userstuff)
            self._summary += msg
            msg += "Final output will be saved in \n'{}'\n".format(self.details.output_job)
            msg += "GATE job submit directory:\n'{}'\n".format(self._RUNGATE_submit_directory)
            msg += "GUI logs:\n'{}'\n".format(syscfg["log file path"])
            logger.info(msg)
            #success = launch_job_control_daemon(self._RUNGATE_submit_directory)
            #if self.details.mc_stat_type == MCStatType.Xpct_unc_in_target:
            ret=os.system( "{bindir}/job_control_daemon.py -l {username} -t {timeout} -n {minprim} -u {uncgoal} -p {poll} -d -w '{workdir}'".format(
                bindir=syscfg['bindir'],
                username=syscfg['username'],
                # DONE: change this into Nprim, Unc, TimeOut settings
                #goal=self.details.mc_stat_thr,
                timeout=self.details.mc_stat_thr[MCStatType.Nminutes_per_job],
                minprim=self.details.mc_stat_thr[MCStatType.Nions_per_beam],
                uncgoal=self.details.mc_stat_thr[MCStatType.Xpct_unc_in_target],
                poll=syscfg['stop on script actor time interval [s]'],
                workdir=self._RUNGATE_submit_directory))
            if ret==0:
                msg="successful start of job statistics daemon"
                self._summary += msg+"\n"
                logger.info(msg)
            else:
                msg="FAILED to start job statistics daemon"
                self._summary += msg+"\n"
                logger.error(msg)
        else:
            msg = "Job submit error: return value {}".format(ret)
            self._summary += msg
            logger.error(msg)
        os.chdir( save_cwd )
        return ret

################################################################################
# UNIT TESTS (would be nice)
################################################################################
#
#import unittest
#import sys
#from system_configuration import get_sysconfig

# vim: set et softtabstop=4 sw=4 smartindent:
