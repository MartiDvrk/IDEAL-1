import ideal_module as idc
import time
import os
import pandas as pd
import yaml
import io


def start_from_df_ideal(df, beamline_override = None, donotstartsimulation=False):
    curUser  = os.getlogin()
    outDir = dict()      

    # run simulations with settings from dataframe
    for index, row in df.iterrows():
        nPart = 0
        unc = 0
        if row['Stat_goal_type']=='numberOfParticles':
            nPart = row['Stat_goal_value']
        elif row['Stat_goal_type']=='uncertainty':
            unc = row['Stat_goal_value']
            
        rp = row["RP_fpath"]
        print(index, rp)
        print(f"start Task ID: {row['TaskID']}")
        if not rp or rp != rp:
            continue
    
        phantomStr = None
        if row["Phantom"] and not (row["Phantom"] != row["Phantom"]):
            phantomStr = row["Phantom"]
            print(f'Start phantom simulation with: {phantomStr}')
        if pd.isnull(row["NumberCores"]):
            n_cores = 0
        else:
            n_cores = int(float(row["NumberCores"]))
        # except:
        #     n_cores = 8
        condor_memory = row["MemoryReqGB"]*1000 ## convert to MB
        statistical_goal = row['Stat_goal_type']
        if (nPart==0) and (statistical_goal == 'numberOfParticles'):
            nPart = 10000 #int(float(row['Stat_goal_value']))
        if (unc==0) and (statistical_goal == 'uncertainty'):
            unc = float(row['Stat_goal_value'])
        if (nPart==0) and (unc==0):
            raise ValueError(f'Statistical goal is empty, check pickel.')
        mc_simulation = idc.ideal_simulation(curUser, RP_path = rp.strip(), n_particles = nPart, uncertainty = unc, phantom = phantomStr, n_cores = n_cores, beamline_override=beamline_override,condor_memory = condor_memory)
        if donotstartsimulation:
            print("Debug no simulation started")
        else:
            mc_simulation.start_simulation()
            taskID = row["TaskID"]
            outDir.update({taskID: mc_simulation.outputdir})
        time.sleep(60)
            
    return outDir

if __name__ == '__main__':
    
    # initialize system configuration object:
    sysconfig = idc.initialize_sysconfig(filepath='',username='',debug=True)
    prefix="\n * "
    
    # read data frame    
    beamline = 'IR2HBLc'
    beamline_override = None
    testName = 'test_ideal_refactored'

   # read data frame    
    pklFpath =f'/home/ideal/0_Data/01_BaselineData/{beamline}/{beamline}_db.pkl'
    dfhbl = pd.read_pickle(pklFpath)

    L = dfhbl['TaskID'].isin(['IR2HBLc_1.1.5']) #,'IR2HBLc_1.1.5','IR2HBLc_2.1.1','IR2HBLc_2.1.5',
                              #'IR2HBLc_3.1.1','IR2HBLc_3.1.5','IR2HBLc_1.4.1'])

    subdf = dfhbl[L]

    print(subdf[['TaskID','RP_fpath']])

    outDir = start_from_df_ideal(subdf, beamline_override = beamline_override)
    final_yml_dict = {'test beammodel': testName,
                      'original beamline': beamline,
                      'output paths':outDir}

    outFile = f'/home/fava/Data/yml/IDEAL_v1.2/{beamline}/{testName}.yml'
    
    with io.open(outFile, 'w', encoding='utf8') as outfile:
        yaml.dump(final_yml_dict, outfile, default_flow_style=False, allow_unicode=True)

    print('\n\n Finished! \n\n\n')