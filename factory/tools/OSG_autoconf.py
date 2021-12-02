#!/usr/bin/env python3
""" Allows to retrieve information from the OSG collector and generate the factory xml file
"""




import os
import sys
import copy
import logging
import argparse
import fractions

import htcondor

from glideinwms.lib.config_util import ENTRY_STUB, GLIDEIN_SUPPORTED_VO_MAP, ProgramError, get_attr_str, update, \
     get_yaml_file_info, write_to_yaml_file, get_submit_attr_str, write_to_xml_file, get_limits_str, \
     get_submission_speed, BEST_FIT_TAG


def load_config():
    """Load few parameters from the configuration file
    """
    parser = argparse.ArgumentParser(prog='OSG_autoconf')
    parser.add_argument('config', nargs=1, help='The configuration file')
    args = parser.parse_args()

    config = get_yaml_file_info(args.config[0])

    return config


def get_vos(allowed_vos):
    """This function converts the list of VO from the collector to the frontend ones

    Args:
        allowed_vos (list): The list ov vos in the OSG collector

    Returns:
        set: The set of frontend VOs to add to the configuration GLIDEIN_SupportedVOs
    """
    vos = set()
    for vorg in allowed_vos:
        if vorg in GLIDEIN_SUPPORTED_VO_MAP:
            vos.add(GLIDEIN_SUPPORTED_VO_MAP[vorg])
#        else:
#            print(vorg + " VO is not in GLIDEIN_Supported_VOs_map")

    return vos


def get_bestfit_pilot(celem, resource):
    """ Site admins did not specify a pilot section. Let's go through the resource catalog sections
        and find the pilot parameters that best fit the CE.

        Args:
            celem (list): List of resource catalog dictionaries as returned by the OSG collector

        Returns:
            dict: A dictionary to be used to generate the xml for this CE
    """
    vos = set()
    memory = sys.maxsize
    walltime = sys.maxsize
    cpus = ""
    for osg_catalog in celem["OSG_ResourceCatalog"]:
        if "AllowedVOs" in osg_catalog:
            if len(vos) == 0:
                vos = get_vos(osg_catalog["AllowedVOs"])
            else:
                vos = vos.intersection(get_vos(osg_catalog["AllowedVOs"]))
        if "Memory" in osg_catalog:
            memory = min(memory, osg_catalog["Memory"])
        if "MaxWallTime" in osg_catalog:
            walltime = min(walltime, osg_catalog["MaxWallTime"])
        if "CPUs" in osg_catalog:
            if cpus == "":
                cpus = osg_catalog["CPUs"]
            else:
                cpus = fractions.gcd(cpus, osg_catalog["CPUs"])

    return get_entry_dictionary(resource, vos, cpus, walltime, memory)


def get_pilot(resource, pilot_entry):
    """ Site admins specified a pilot entry section in the OSG configure file. Prepare
        the xml pilot dictionary based on the OSG collector information

        Returns:
            dict: A dictionary to be used to generate the xml for this pilot entry
    """
    vos = pilot_entry.get("AllowedVOs", set())
    cpus = pilot_entry.get("CPUs", "")
    walltime = pilot_entry.get("MaxWallTime", "")
    memory = pilot_entry.get("Memory", "")

    res = get_entry_dictionary(resource, vos, cpus, walltime, memory)

    if "MaxPilots" in pilot_entry:
        res["limits"] = {'entry' : {'glideins' : pilot_entry["MaxPilots"]}}
    if "GPUs" in pilot_entry:
        res["submit_attrs"]["Request_GPUs"] = pilot_entry["GPUs"]
    if pilot_entry.get("RequireSingularity") is False and "OS" in pilot_entry:
        res["attrs"]["GLIDEIN_REQUIRED_OS"] = {'value' : pilot_entry["OS"]}
    if "WholeNode" in pilot_entry and pilot_entry["WholeNode"]:
        res["submit_attrs"]["+WantWholeNode"] = pilot_entry["WholeNode"]
        if "GLIDEIN_CPUS" in res["attrs"]:
            del res["attrs"]["GLIDEIN_CPUS"]
        if "GLIDEIN_MaxMemMBs" in res["attrs"]:
            del res["attrs"]["GLIDEIN_MaxMemMBs"]
        if "+maxWallTime" in res["submit_attrs"]:
            del res["submit_attrs"]["+maxWallTime"]
        if "+maxMemory" in res["submit_attrs"]:
            del res["submit_attrs"]["+maxMemory"]

    return res


def get_entry_dictionary(resource, vos, cpus, walltime, memory):
    """ Utility function that converts some variable into an xml pilot dictionary
    """
    # Assigning this to an entry dict variable to shorten the line
    edict = {} # Entry dict
    edict["gridtype"] = "condor"
    edict["attrs"] = {}
    edict["attrs"]["GLIDEIN_Site"] = {"value": resource}
    if resource:
        edict["attrs"]["GLIDEIN_ResourceName"] = {"value": resource}
    if len(vos) > 0:
        edict["attrs"]["GLIDEIN_Supported_VOs"] = {"value": ",".join(vos)}
#     else:
#         print(gatekeeper + " CE does not have VOs")
    edict["submit_attrs"] = {}
    if cpus != "":
        edict["attrs"]["GLIDEIN_CPUS"] = {"value": cpus}
        edict["submit_attrs"]["+xcount"] = cpus
    if walltime != sys.maxsize:
        glide_walltime = walltime * 60 - 1800
        edict["attrs"]["GLIDEIN_Max_Walltime"] = {"value": glide_walltime}
        edict["submit_attrs"]["+maxWallTime"] = walltime
    if memory != sys.maxsize:
        edict["attrs"]["GLIDEIN_MaxMemMBs"] = {"value": memory}
        edict["submit_attrs"]["+maxMemory"] = memory
    return edict


def get_information(host):
    """Query the OSG collector and get information about the known HTCondor-CE.

    The OSG collector is queried with the -sched option to get information about
    all the HTCondorCEs. The relevant OSG resource information is then organized
    in a dict.

    Args:
        host (str): The hostname where the OSG collector is running

    Returns:
        dict: A resource dictionary whose keys are the resources ('cedar', 'UCHICAGO', 'NMSU')
            and its values are the needed information to create the entry. For example:

            {'hosted-ce32.grid.uchicago.edu':
                     {'DEFAULT_ENTRY': {'attrs': {'GLIDEIN_CPUS': {'value': 16L},
                                                  'GLIDEIN_MaxMemMBs': {'value': 163840L},
                                                  'GLIDEIN_Max_Walltime': {'value': 171000L},
                                                  'GLIDEIN_ResourceName': {'value': 'NEMO'},
                                                  'GLIDEIN_Site': {'value': 'OSG_US_UWM_NEMO'},
                                                  'GLIDEIN_Supported_VOs': {'value': 'OSGVO'}},
                                        'gridtype': 'condor',
                                        'submit_attrs': {'+maxMemory': 163840L,
                                                         '+maxWallTime': 2880L,
                                                         '+xcount': 16L}}}}
    """
    collector = htcondor.Collector(host)
    ces = collector.query(htcondor.AdTypes.Schedd, projection=["Name", "OSG_ResourceGroup", "OSG_Resource", "OSG_ResourceCatalog"])
    return get_information_internal(ces)


def get_information_internal(ces):
    """ Query the OSG collector and get information about the known HTCondor-CE (internal function)
    """
    result = {}
    entry = "DEFAULT_ENTRY"
    for celem in ces:
        if "OSG_ResourceGroup" in celem:
            resource = celem["OSG_ResourceGroup"] or celem["OSG_Resource"]
            site = celem["OSG_Resource"] # not used for now, but factory ops will add a new attribute 1in the future
            gatekeeper = celem["Name"].lower()
            if resource:
                result.setdefault(resource, {})[gatekeeper] = {}
                if "OSG_ResourceCatalog" in celem:
                    pilot_entries = [osg_catalog for osg_catalog in celem["OSG_ResourceCatalog"] if osg_catalog.get('IsPilotEntry') is True]
                    requires_bestfit = pilot_entries == []
                    if requires_bestfit:
                        result[resource][gatekeeper].setdefault(BEST_FIT_TAG, {})[entry] = get_bestfit_pilot(celem, resource)
                    else:
                        for pentry in pilot_entries:
                            result[resource][gatekeeper].setdefault(pentry["Name"], {})[entry] = get_pilot(resource, pentry)
                else:
                    print(gatekeeper + " CE does not have OSG_ResourceCatalog attribute")
            else:
                print(gatekeeper + " CE does not have OSG_ResourceGroup attribute")

    return result


def get_entries_configuration(data):
    """Given the dictionary of resources, returns the generated factory xml file

    Args:
        data (dict): A dictionary similar to the one returned by ``get_information``

    Returns:
        str: The factory xml file as a string
    """
    entries_configuration = ""
    for _, site_information in sorted(data.items()):
        for celem, ce_information in sorted(site_information.items()):
            for _, q_information in sorted(ce_information.items()):
                for entry, entry_information in sorted(q_information.items()):
                    entry_configuration = copy.deepcopy(entry_information)
                    entry_configuration["entry_name"] = entry
                    # Can we get these information (next key)?
                    entry_configuration["attrs"]["GLIDEIN_REQUIRED_OS"] = (
                        {"comment": "This value has been hardcoded", "value": "any"}
                    )
                    # Probably we can use port from attribute AddressV1 or CollectorHost
                    entry_configuration["gatekeeper"] = celem + " " + celem + ":9619"
                    entry_configuration["rsl"] = ""
                    entry_configuration["attrs"] = get_attr_str(entry_configuration["attrs"])
                    if "submit_attrs" in entry_configuration:
                        entry_configuration["submit_attrs"] = (
                            get_submit_attr_str(entry_configuration["submit_attrs"])
                        )
                    else:
                        entry_configuration["submit_attrs"] = ""
                    entry_configuration["limits"] = get_limits_str(entry_configuration["limits"])
                    entry_configuration["submission_speed"] = get_submission_speed(entry_configuration["submission_speed"])
                    entries_configuration += ENTRY_STUB % entry_configuration

    return entries_configuration


def backward_fix(out):
    """ special backward compatibility case. Would like to remove this once configs are fixed.
    """
    for site, site_information in out.items():
        if site_information is None: continue
        for ce_hostname, ce_information in site_information.items():
            if ce_information is None: continue
            for qelem, q_information in ce_information.items():
                if (q_information is None or
                        set(['limits', 'attrs', 'submit_attrs']) & set(q_information.keys())):
                    print("\033[91mMissing 'queue' level for site %s and CE %s. Fixing it up for you, but please add a '%s' layer!!\033[0m" %
                          (site, ce_hostname, BEST_FIT_TAG))
                    out[site][ce_hostname].setdefault(BEST_FIT_TAG, {})[qelem] = q_information
                    del out[site][ce_hostname][qelem]


def is_true(param):
    """Determine if the parameter passed as argument is true or false

    Args:
        param: the parameter we need to determine if it is True or False. Can be any type.

    Returns:
        bool: True if the the string representation of param is "true"
    """

    return str(param).lower() == 'true'


def merge_yaml(config, white_list):
    """Merges different yaml file and return the corresponding resource dictionary

    Three different yam files are merged. First we read the factory white list/override file that
    contains the list of entries operators want to generate, with the parameters they want to
    override.
    Then the yaml generated from the OSG collector and the default yaml file are read.
    For each entry all the operator information are "updated" with the information coming from
    the collector first, and from the default file next.

    Returns:
        dict: a dict similar to the one returned by ``get_information``, but with all the defaults
            and the operators overrides in place (only whitelisted entries are returned).
    """
    out = get_yaml_file_info(white_list)
    backward_fix(out)
    osg_info = get_yaml_file_info(config["OSG_YAML"])
    missing_info = get_yaml_file_info(config["MISSING_YAML"])
    update(osg_info, missing_info)
    default_information = get_yaml_file_info(config["OSG_DEFAULT"])
    #TODO remove this if once factory ops trims the default file
    if 'DEFAULT_ENTRY' not in default_information: # fixup default file, I'd like to trim it down
        default_information = default_information['DEFAULT_SITE']['DEFAULT_GETEKEEPER']
    for site, site_information in out.items():
        if site_information is None:
            print("There is no site information for %s site in white list file. Skipping it." % site)
            del out[site]
            continue
        print("Merging %s" % site)
        if site not in osg_info:
            print("You put %s in the whitelist file, but the site is not present in the collector or the missing %s file"
                  % (site, config["MISSING_YAML"]))
            raise ProgramError(2)
        for ce_hostname, ce_information in site_information.items():
            if ce_information is None:
                print("There is no CE information for %s CE in white list file. Skipping it." % ce_hostname)
                del out[site][ce_hostname]
                continue
            if ce_hostname not in osg_info[site]:
                print("Working on whitelisted site %s: cant find ce %s in the generated %s or the missing %s files "
                      % (site, ce_hostname, config["OSG_YAML"], config["MISSING_YAML"]))
                raise ProgramError(3)
            for qelem, q_information in ce_information.items():
                for entry, entry_information in q_information.items():
                    if entry_information is None:
                        out[site][ce_hostname][qelem][entry] = osg_info[site][ce_hostname][qelem]["DEFAULT_ENTRY"]
                        entry_information = out[site][ce_hostname][qelem][entry]
                    else:
                        update(entry_information, osg_info[site][ce_hostname][qelem]["DEFAULT_ENTRY"],
                               overwrite=False)
                    if osg_info[site][ce_hostname][qelem]["DEFAULT_ENTRY"]["gridtype"] == "condor":
                        if "attrs" in entry_information and entry_information["attrs"]:
                            entry_information = update_submit_attrs(entry_information, "GLIDEIN_CPUS", "+xcount")
                            entry_information = update_submit_attrs(entry_information, "GLIDEIN_MaxMemMBs", "+maxMemory")
                            entry_information = update_submit_attrs(entry_information, "GLIDEIN_Max_Walltime", "+maxWallTime")
                        if "submit_attrs" in entry_information:
                            if "+WantWholeNode" in entry_information["submit_attrs"]:
                                want_whole_node = entry_information["submit_attrs"]["+WantWholeNode"]
                                if is_true(want_whole_node):
                                    entry_information = set_whole_node_entry(entry_information)
                            if "Request_GPUs" in entry_information["submit_attrs"]:
                                entry_information = set_gpu_entry(entry_information)
                    if "limits" in entry_information:
                        if "entry" in entry_information["limits"] and "frontend" not in entry_information["limits"]:
                            entry_information["limits"]["frontend"] = entry_information["limits"]["entry"]
                        elif "entry" not in entry_information["limits"] and "frontend" in entry_information["limits"]:
                            entry_information["limits"]["entry"] = entry_information["limits"]["frontend"]
                    update(
                        entry_information,
                        default_information["DEFAULT_ENTRY"],
                        overwrite=False
                    )
    return out


def set_gpu_entry(entry_information):
    """Set gpu entry

    Args:
        entry_information (dict): a dictionary of entry information from white list file

    Returns:
        dict: a dictionary of entry information from white list file with gpu settings
    """
    if "attrs" not in entry_information:
        entry_information["attrs"] = {}
    if "GLIDEIN_Resource_Slots" not in entry_information["attrs"]:
        entry_information["attrs"]["GLIDEIN_Resource_Slots"] = {"value": "GPUs," + str(entry_information["submit_attrs"]["Request_GPUs"]) + ",type=main"}

    return entry_information


def set_whole_node_entry(entry_information):
    """Set whole node entry

    Args:
        entry_information (dict): a dictionary of entry information from white list file

    Returns:
        dict: a dictionary of entry information from white list file with whole node settings
    """
    if "submit_attrs" in entry_information:
        if "+xcount" not in entry_information["submit_attrs"]:
            entry_information["submit_attrs"]["+xcount"] = None
        if "+maxMemory" not in entry_information["submit_attrs"]:
            entry_information["submit_attrs"]["+maxMemory"] = None

    if "attrs" not in entry_information:
        entry_information["attrs"] = {}
    if "GLIDEIN_CPUS" not in entry_information["attrs"]:
        entry_information["attrs"]["GLIDEIN_CPUS"] = {"value": "auto"}
    if "GLIDEIN_ESTIMATED_CPUS" not in entry_information["attrs"]:
        entry_information["attrs"]["GLIDEIN_ESTIMATED_CPUS"] = {"value": 32}
    if "GLIDEIN_MaxMemMBs" not in entry_information["attrs"]:
        entry_information["attrs"]["GLIDEIN_MaxMemMBs"] = {"type": "string", "value": ""}
    if "GLIDEIN_MaxMemMBs_Estimate" not in entry_information["attrs"]:
        entry_information["attrs"]["GLIDEIN_MaxMemMBs_Estimate"] = {"value": "True"}

    return entry_information


def update_submit_attrs(entry_information, attr, submit_attr):
    """Update submit attribute according to produced attribute if submit attribute is not defined

    Args:
        entry_information (dict): a dictionary of entry information from white list file
        attr (str): attribute name
        submit_attr (str): submit attribute name

    Returns:
        dict: a dictionary of entry information from white list file with possible updated submit attribute
    """
    if attr in entry_information["attrs"] and entry_information["attrs"][attr]:
        if "submit_attrs" in entry_information:
            if submit_attr not in entry_information["submit_attrs"]:
                if attr == "GLIDEIN_Max_Walltime":
                    entry_information["submit_attrs"][submit_attr] = int(entry_information["attrs"][attr]["value"] / 60) + 30
                else:
                    entry_information["submit_attrs"][submit_attr] = entry_information["attrs"][attr]["value"]
        else:
            entry_information["submit_attrs"] = {}
            if attr == "GLIDEIN_Max_Walltime":
                entry_information["submit_attrs"][submit_attr] = int(entry_information["attrs"][attr]["value"] / 60) + 30
            else:
                entry_information["submit_attrs"][submit_attr] = entry_information["attrs"][attr]["value"]

    return entry_information


def create_missing_file(config, osg_collector_data):
    """ Create the missing yaml file.
    """
    new_missing = {}
    try:
        osg_info = get_yaml_file_info(config["OSG_YAML"])
    except ProgramError:
        write_to_yaml_file(config["MISSING_YAML"], new_missing)
        print("Skipping verification of missing files since OSG.yml does not exist. Is this the first time you run OSG_autoconf?")
        return
    missing_info = get_yaml_file_info(config["MISSING_YAML"]) if os.path.isfile(config["MISSING_YAML"]) else {}

    print("Verifying missing sites and CEs")
    for white_list in sorted(config["OSG_WHITELISTS"]):
        print("Checking if any site or CE in %s is missing in the OSG collector" % white_list)
        whitelist_info = get_yaml_file_info(white_list)
        tmp = create_missing_file_internal(missing_info, osg_info, whitelist_info, osg_collector_data)
        update(new_missing, tmp)

    write_to_yaml_file(config["MISSING_YAML"], new_missing)


def create_missing_file_internal(missing_info, osg_info, whitelist_info, osg_collector_data):
    """ Create the missing yaml file (internal function).
    """
    new_missing = {}
    for site, site_information in whitelist_info.items():
        if site_information is None:
            continue
        if site not in osg_collector_data: # Check if the site disappeared from the OSG collector
            if site in osg_info or site in missing_info:
                print("WARNING! Site %s is in the whitelist file, but not in the collector. Retrieving it from old data (old OSG YAML or MISSING YAML), and saving it to the MISSING YAML" % site)
                new_missing[site] = osg_info.get(site) or missing_info[site]
            else:
                print("ERROR! Site %s is in the whitelist file, and I cant neither find it in the OSG YAML saved data, nor the MISSING YAML" % site)
            continue
        for celem, ce_information in site_information.items():
            if ce_information is None:
                continue
            if celem not in osg_collector_data[site]:
                if celem in osg_info.get(site, {}) or celem in missing_info.get(site, {}):
                    print("WARNING! CE %s of site %s is in the whitelist file, but not in the collector. Retrieving it from old data (old OSG YAML or MISSING YAML), and saving it to the MISSING YAML" % (celem, site))
                    new_missing.setdefault(site, {})
                    new_missing[site][celem] = osg_info.get(site, {}).get(celem, False) or missing_info[site][celem]
                else:
                    print("ERROR! CE %s of site %s is in the whitelist file, and I cant neither find it in the OSG YAML saved data, nor the MISSING YAML" % (celem, site))

    # Add the new additional layer if it is missing
    #TODO Remove it once facotry ops is done with this (in 3.7.4)!
    for site, site_information in new_missing.items():
        for celem, ce_information in site_information.items():
            if 'DEFAULT_ENTRY' in ce_information:
                new_missing[site][celem].setdefault(BEST_FIT_TAG, {})['DEFAULT_ENTRY'] = new_missing[site][celem]['DEFAULT_ENTRY']
                del new_missing[site][celem]['DEFAULT_ENTRY']

    # Returning for unit tests
    return new_missing


def main():
    """The main"""
    config = load_config()
    # Queries the OSG collector
    result = get_information(config["OSG_COLLECTOR"])
    # Create the file for the missing CEs
    create_missing_file(config, result)
    # Write the received information to the OSG.yml file
    write_to_yaml_file(config["OSG_YAML"], result)
    # Merges different yaml files: the defaults, the generated one, and the factory overrides
    for white_list in sorted(config["OSG_WHITELISTS"]):
        result = merge_yaml(config, white_list)
        # Convert the resoruce dictionary obtained this way into a string (xml)
        entries_configuration = get_entries_configuration(result)
        # Write the factory configuration file on the disk
        write_to_xml_file(white_list.replace("yml", "xml"), entries_configuration)


if __name__ == "__main__":
    try:
        main()
    except ProgramError as merr:
        print("\033[91mError! \033[0m" + ProgramError.codes_map[merr.code])
        sys.exit(merr.code)
    except:
        logging.exception("")
        print("\033[91mUnexpected exception. Aborting automatic configuration generation!\033[0m")
        raise
    sys.exit(0)
