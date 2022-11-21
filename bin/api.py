#!/usr/bin/env python3
# -----------------------------------------------------------------------------
#   Copyright (C): MedAustron GmbH, ACMIT Gmbh and Medical University Vienna
#   This software is distributed under the terms
#   of the GNU Lesser General  Public Licence (LGPL)
#   See LICENSE for further details
# -----------------------------------------------------------------------------

# generic imports
import os
import zipfile
import configparser
# ideal imports
from ideal_module import *
from utils.condor_utils import remove_condor_job, get_job_daemons, kill_process, zip_files
# api imports
from flask import Flask, request, redirect, jsonify, Response, send_file
from flask_restful import Resource, Api, reqparse
from werkzeug.utils import secure_filename

# Initialize sytem configuration once for all
sysconfig = initialize_sysconfig(username = 'myqaion')
base_dir = sysconfig['IDEAL home']
input_dir = base_dir + '/data/dicom_input/'
#base_dir = '/user/fava/Postman/files'

# api configuration
UPLOAD_FOLDER = base_dir
ALLOWED_EXTENSIONS = {'dcm', 'zip'}

app = Flask(__name__)
api = Api(app)

# List of all active jobs. Members will be simulation objects
jobs_list = dict()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS  
        
def unzip(dir_name):
    extension = ".zip"
    os.chdir(dir_name)
    for item in os.listdir(dir_name):
        if item.endswith(extension):
            file_name = os.path.abspath(item)
            zip_ref = zipfile.ZipFile(file_name) # create zipfile object
            zip_ref.extractall(dir_name)
            zip_ref.close()
            os.remove(file_name)

def read_ideal_job_status(cfg_settings):
    cfg = configparser.ConfigParser()
    cfg.read(cfg_settings)
    status = cfg['DEFAULT']['status']
    
    return status

def generate_input_folder(input_dir,filename,username):
    rp = filename.split('.zip')[0]
    folders = [i for i in os.listdir(input_dir) if (username in i) and (rp in i)]
    index = len(folders)+1
    ID = username + '_' + str(index) + '_' + rp
    # create data dir for the job
    datadir = input_dir + ID
    os.mkdir(datadir)
    
    return datadir, rp
    

if __name__ == '__main__':

    @app.route("/version")
    def version():
        return get_version()
    
    
    @app.route("/jobs", methods=['POST'])
    def start_new_job():        
        # get data from client

        # RP dicom
        if 'dicomRtPlan' not in request.files:
            return Response("{dicomRtPlan':'missing key'}", status=400, mimetype='application/json')
        rp_file = request.files['dicomRtPlan']
        if rp_file.filename == '':
            return Response("{dicomRtPlan':'missing file'}", status=400, mimetype='application/json')
        rp_filename = secure_filename(rp_file.filename)
        
        # RS dicom
        if 'dicomStructureSet' not in request.files:
            return Response("{dicomStructureSet':'missing key'}", status=400, mimetype='application/json')
        rs_file = request.files['dicomStructureSet']
        if rs_file.filename == '':
            return Response("{dicomStructureSet':'missing file'}", status=400, mimetype='application/json')
        
        # CT dicom
        if 'dicomCTs' not in request.files:
            return Response("{dicomCTs':'missing key'}", status=400, mimetype='application/json')
        ct_file = request.files['dicomCTs']
        if ct_file.filename == '':
            return Response("{dicomCTs':'missing file'}", status=400, mimetype='application/json')
        
        # RD dicom
        if 'dicomRDose' not in request.files:
            return Response("{dicomRDose':'missing key'}", status=400, mimetype='application/json')
        rd_file = request.files['dicomRDose']
        if rd_file.filename == '':
            return Response("{dicomRDose':'missing file'}", status=400, mimetype='application/json')
      
        # username
        arg_username = request.form.get('username')
        if arg_username is None:
            return Response("{username':'missing'}", status=400, mimetype='application/json')
        
        # stopping criteria
        arg_number_of_primaries_per_beam = request.form.get('numberOfParticles')
        if arg_number_of_primaries_per_beam is None:
            arg_number_of_primaries_per_beam = 0
        else: 
            arg_number_of_primaries_per_beam = int(request.form.get('numberOfParticles'))

        arg_percent_uncertainty_goal = request.form.get('uncertainty')
        if arg_percent_uncertainty_goal is None:
            arg_percent_uncertainty_goal = 0
        else:
            arg_percent_uncertainty_goal = float(arg_percent_uncertainty_goal)
            
        if arg_percent_uncertainty_goal == 0 and arg_number_of_primaries_per_beam == 0:
            return Response("{stoppingCriteria':'missing'}", status=400, mimetype='application/json')
    
        # TODO: get checksum of config files and compare it to our checksome
        datadir, rp = generate_input_folder(input_dir,rp_filename,arg_username)
        app.config['UPLOAD_FOLDER'] = datadir
        
        #save files in folder
        rp_file.save(os.path.join(datadir,secure_filename(rp_file.filename)))
        rs_file.save(os.path.join(datadir,secure_filename(rs_file.filename)))
        ct_file.save(os.path.join(datadir,secure_filename(ct_file.filename)))
        rd_file.save(os.path.join(datadir,secure_filename(rd_file.filename)))
        
        # unzip dicom data
        unzip(datadir)
        
        # create simulation object
        dicom_file = datadir + '/' + rp
        sysconfig.override('username',arg_username)
        mc_simulation = ideal_simulation(arg_username,dicom_file,n_particles = arg_number_of_primaries_per_beam,
                                         uncertainty=arg_percent_uncertainty_goal)
        
        # check dicom files
        ok, missing_keys = mc_simulation.verify_dicom_input_files()
        
        if not ok:
            return Response(missing_keys, status=400, mimetype='application/json')
        
        # Get job UID
        jobID = mc_simulation.outputdir.split("/")[-1]
        
        # start simulation and append to list  
        mc_simulation.start_simulation()
        jobs_list[jobID] = mc_simulation
        
        # check stopping criteria
        mc_simulation.start_job_control_daemon()

                
        return jobID

    @app.route("/jobs/<jobId>", methods=['DELETE','GET'])
    def stop_job(jobId):
        if jobId not in jobs_list:
            return Response('Job does not exist', status=404, mimetype='string')
            #return '', 400
        if request.method == 'DELETE':
            args = request.args
            cancellation_type = args.get('cancelationType')
            # set default to soft
            if cancellation_type is None:
                cancellation_type = 'soft'
            if cancellation_type not in ['soft', 'hard']:
                return Response('CancelationType not recognized, choose amongst: soft, hard', status=400, mimetype='string')
            
            cfg_settings = jobs_list[jobId].settings
            status = read_ideal_job_status(cfg_settings)
            
            if status == 'FINISHED':
                return Response('Job already finished', status=199, mimetype='string')
    
            if cancellation_type=='soft':
                simulation = jobs_list[jobId]
                simulation.soft_stop_simulation(simulation.cfg)
            if cancellation_type=='hard':
                condorId = jobs_list[jobId].condor_id
                remove_condor_job(condorId)
            
            # kill job control daemon
            daemons = get_job_daemons('job_control_daemon.py')
            kill_process(daemons[simulation.workdir])
            
            return cancellation_type
        
        if request.method == 'GET':
            # Transfer output result upon request
            cfg_settings = jobs_list[jobId].settings
            status = read_ideal_job_status(cfg_settings)
            
            if status != 'FINISHED':
                return Response('Job not finished yet', status=409, mimetype='string')
            
            outputdir = jobs_list[jobId].outputdir
            os.chdir(outputdir)
            for file in os.listdir(outputdir):
                # for now we pass only the dcm with the simulated full plan and the report .cfg
                if 'PLAN' in file and '.dcm' in file:
                    monteCarloDoseDicom = file
                if '.cfg' in file:
                    logFile = file
            zip_fn = "output.zip"
            zip_files(zip_fn,[monteCarloDoseDicom,logFile])
            
            return send_file(outputdir+"/"+zip_fn,
                            mimetype = 'zip',
                            download_name= 'output.zip',
                            as_attachment = True)
    
    @app.route("/jobs/<jobId>/status", methods=['GET'])
    def get_status(jobId):
        if jobId not in jobs_list:
            return Response('Job does not exist', status=404, mimetype='string')
            #return '', 400
        
        cfg_settings = jobs_list[jobId].settings
        status = read_ideal_job_status(cfg_settings)
        return jsonify({'status': status})
        


    app.run()
    

    

# vim: set et softtabstop=4 sw=4 smartindent:
