from io import BytesIO
from user_agents import parse
from requests import get
from urllib.parse import unquote

import geoip2.database
import urllib.parse
import collections
import psycopg2
import datetime
import tarfile
import hashlib
import logging
import base64
import boto3
import gzip
import json
import copy
import sys
import re
import os



######## Functions declaration
def flatten(d, parent_key='', sep='.'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)

_reg = re.compile(r'(?!^)(?<!_)([A-Z])')

def camel_to_snake(s):
    return _reg.sub(r'_\1', s).lower()

### Print with tag and timestamp
def tprint(tag, txt):
    ts = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print("[" + str(tag) + "][" + str(ts) + "] " + txt)


######## Initialize Logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

######## Initialize S· connection
s3 = boto3.client('s3')

################## Lambda function

def lambda_handler(event, context):

    error_count = 0

    ########################
    #### Downloading the new RAW events from S3

    bucket_name = event['Records'][0]['s3']['bucket']['name']
    file_key = event['Records'][0]['s3']['object']['key']
    logger.info('Reading {} from {}'.format(file_key, bucket_name))
    s3.download_file(bucket_name, file_key, '/tmp/file.zip')

    ########################
    #### Getting geolocation DB file
    geoippath = '/tmp/GeoLite2-City.mmdb'
    try:
        s3.download_file(bucket_name, 'GeoLite2-City.mmdb', '/tmp/GeoLite2-City.mmdb')
    except:
        url = "https://geolite.maxmind.com/download/geoip/database/GeoLite2-City.tar.gz" # This link no longer works. TODO: Check alternatives
        response = get(url)
        with open('/tmp/GeoLite2-City.tar.gz', 'wb') as file:
            file.write(response.content)
        geofilename = re.compile("GeoLite2-City.mmdb")
        tar = tarfile.open("/tmp/GeoLite2-City.tar.gz")
        for member in tar.getmembers():
            if geofilename.search(member.name):
                geoippath = '/tmp/' + member.name
                tar.extract(member, path='/tmp/')
        tar.close()
        s3.upload_file(geoippath, bucket_name, 'GeoLite2-City.mmdb')

    
    ########################
    #### Getting column names for all tables in atomic schema

    conn = psycopg2.connect(host=os.environ['POSTGRES_HOST'], database=os.environ['POSTGRES_DATABASE'], user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'])
    sql = "SELECT t.table_schema, t.table_name, c.column_name FROM information_schema.tables t JOIN INFORMATION_SCHEMA.COLUMNS c ON c.table_name = t.table_name WHERE t.table_schema='atomic' ORDER BY t.table_name, c.ordinal_position ;"

    cur = conn.cursor()
    cur.execute(sql)
    table_list = cur.fetchall()

    cur.close()
    conn.close()

    table_columns = {}
    for e in table_list:
        if not e[1] in table_columns.keys():
            table_columns[e[1]] = []
        table_columns[e[1]].append(e[2])


    ########################
    #### Loading raw events content

    archgz = gzip.open('/tmp/file.zip')
    file_content = archgz.read()
    lines = file_content.split(b'\n')


    ########################
    #### Processing entries and storing them

    header = re.search ('#Fields: (.*)',lines[1].decode("utf-8"))
    header = header.group(1).split()

    tprint(file_key, "Processing and enriching raw entries")
    try:
        datvalues = ""
        all_events = []
        geoipdbreader = geoip2.database.Reader(geoippath)
        i = 0
        for l in lines[2:-1]:

            r = re.compile(r'([^\t]*)\t*')
            l = r.findall(l.decode("utf-8"))[:-1]
            collector_tstamp = l[0] + ' ' + l[1]
            refersplitter = re.compile(r'([^/]*)/*')
            referer = refersplitter.findall(l[9])[:-1]
            refr_urlscheme = referer[0][:-1]
            try:
                refr_urlhost = referer[1]
            except:
                refr_urlhost = '-'
            try:
                refr_urlpath = '/' + '/'.join(referer[2:])
            except:
                refr_urlpath = '-'
            querysplitter = re.compile(r'([^\?]*)\?*')
            qryurl = querysplitter.findall(referer[-1])[:-1]
            try:
                refr_urlquery = qryurl[1]
            except IndexError:
                refr_urlquery = '-'
            userag = l[10].replace("%2520", " ")
            useragent = userag
            userag = parse(userag)
            br_name = userag.browser.family + ' ' + userag.browser.version_string
            br_family = userag.browser.family
            br_version = userag.browser.version
            os_family = userag.os.family
            dvce_type = userag.device.family
            dvce_ismobile = userag.is_mobile
            
            user_ipaddress = l[4]
            
            #### We determine geolocation info based on user IP.
            #### Set to NULL if no info available on DB
            try:
                geoipdbresult = geoipdbreader.city(l[4])
                geo_country = geoipdbresult.registered_country.iso_code
                if geo_country is None:
                    geo_country = ''
                try:
                    geo_city = geoipdbresult.city.names['en']
                except:
                    geo_city = '-'
                geo_zipcode = geoipdbresult.postal.code
                geo_latitude = geoipdbresult.location.latitude
                geo_longitude = geoipdbresult.location.longitude
                try:
                    geo_region_name = geoipdbresult.subdivisions[0].names['en']
                except:
                    geo_region_name = '-'
                geo_timezone = geoipdbresult.location.time_zone
            except:
                geo_country = ''
                geo_city = ''
                geo_zipcode = ''
                geo_latitude = ''
                geo_longitude = ''
                geo_region_name = ''
                geo_timezone = ''

            # In the rare case latitudes and longitudes are set to None, we reset them to '' (later NULL) to avoid insertion errors
            if geo_latitude is None:
                geo_latitude = ''
            if geo_longitude is None:
                geo_longitude = ''

            urisplt = re.compile(r'([^&]*)&*')
            urispltnodes = urisplt.findall(l[11])[:-1]

            user_ipaddress = hashlib.sha224(user_ipaddress.encode('utf-8')).hexdigest() # We store the IP as a hash for privacy
            spvalues = {'app_id': '-','platform': '-','collector_tstamp': collector_tstamp,'dvce_created_tstamp': '-','event': '-','event_id': '-','txn_id': '-','name_tracker': '-','v_tracker': '-','user_id': '-','user_ipaddress': user_ipaddress,'user_fingerprint': '-','domain_userid': '-','domain_sessionidx': '-','network_userid': '-','geo_country': geo_country,'geo_city': geo_city,'geo_zipcode': geo_zipcode,'geo_latitude': geo_latitude,'geo_longitude': geo_longitude,'geo_region_name': geo_region_name,'page_url': '-','page_title': '-','page_referrer': '-','refr_urlscheme': refr_urlscheme,'refr_urlhost': refr_urlhost,'refr_urlpath': refr_urlpath,'refr_urlquery': refr_urlquery,'se_category': '-','se_action': '-','se_label': '-','se_property': '-','se_value': '-','unstruct_event': '-','tr_orderid': '-','tr_affiliation': '-','tr_total': '-','tr_tax': '-','tr_shipping': '-','tr_city': '-','tr_state': '-','tr_country': '-','ti_orderid': '-','ti_sku': '-','ti_name': '-','ti_category': '-','ti_price': '-','ti_quantity': '-','pp_xoffset_min': '-','pp_xoffset_max': '-','pp_yoffset_min': '-','pp_yoffset_max': '-','useragent': unquote(unquote(useragent)),'br_name': br_name,'br_family': br_family,'br_version': br_version,'br_lang': '-','br_features_pdf': '-','br_features_flash': '-','br_features_java': '-','br_features_director': '-','br_features_quicktime': '-','br_features_realplayer': '-','br_features_windowsmedia': '-','br_features_gears': '-','br_features_silverlight': '-','br_cookies': '-','br_colordepth': '-','br_viewwidth': '-','br_viewheight': '-','os_family': os_family,'os_timezone': '-','dvce_type': dvce_type,'dvce_ismobile': dvce_ismobile,'dvce_screenwidth': '-','dvce_screenheight': '-','doc_charset': '-','doc_width': '-','doc_height': '-','tr_currency': '-','ti_currency': '-','geo_timezone': geo_timezone,'dvce_sent_tstamp': '-','domain_sessionid': '-','event_vendor': '-'}
            
                
            if len(urispltnodes[0]) > 3:
                for spparams in urispltnodes:
                    spsplitter = re.compile(r'([^=]*)=*')
                    sp = spsplitter.findall(spparams)[:-1]
                    if sp[0] == 'stm':
                        spvalues['dvce_sent_tstamp'] = sp[1]
                    if sp[0] == 'e':
                        spvalues['event'] = sp[1]
                    if sp[0] == 'url':
                        spvalues['page_url'] = unquote(unquote(sp[1]))
                    if sp[0] == 'page':
                        spvalues['page_title'] = sp[1]
                    if sp[0] == 'pp_mix':
                        spvalues['pp_xoffset_min'] = sp[1]
                    if sp[0] == 'pp_max':
                        spvalues['pp_xoffset_max'] = sp[1]
                    if sp[0] == 'pp_miy':
                        spvalues['pp_yoffset_min'] = sp[1]
                    if sp[0] == 'pp_may':
                        spvalues['pp_yoffset_max'] = sp[1]
                    if sp[0] == 'tv':
                        spvalues['v_tracker'] = sp[1]
                    if sp[0] == 'tna':
                        spvalues['name_tracker'] = sp[1]
                    if sp[0] == 'aid':
                        spvalues['app_id'] = sp[1]
                    if sp[0] == 'p':
                        spvalues['platform'] = sp[1]
                    if sp[0] == 'tz':
                        spvalues['os_timezone'] = unquote(unquote(sp[1]))
                    if sp[0] == 'lang':
                        spvalues['br_lang'] = sp[1]
                    if sp[0] == 'cs':
                        spvalues['doc_charset'] = sp[1]
                    if sp[0] == 'f_pdf':
                        spvalues['br_features_pdf'] = sp[1]
                    if sp[0] == 'f_qt':
                        spvalues['br_features_quicktime'] = sp[1]
                    if sp[0] == 'f_realp':
                        spvalues['br_features_realplayer'] = sp[1]
                    if sp[0] == 'f_wma':
                        spvalues['br_features_windowsmedia'] = sp[1]
                    if sp[0] == 'f_dir':
                        spvalues['br_features_director'] = sp[1]
                    if sp[0] == 'f_fla':
                        spvalues['br_features_flash'] = sp[1]
                    if sp[0] == 'f_java':
                        spvalues['br_features_java'] = sp[1]
                    if sp[0] == 'f_gears':
                        spvalues['br_features_gears'] = sp[1]
                    if sp[0] == 'f_ag':
                        spvalues['br_features_silverlight'] = sp[1]
                    if sp[0] == 'res':
                        ressplitter = re.compile(r'([^x]*)x*')
                        res = ressplitter.findall(sp[1])[:-1]
                        spvalues['dvce_screenheight'] = res[1]
                        spvalues['dvce_screenwidth'] = res[0]
                        continue
                    if sp[0] == 'cd':
                        spvalues['br_colordepth'] = sp[1]
                    if sp[0] == 'cookie':
                        spvalues['br_cookies'] = sp[1]
                    if sp[0] == 'eid':
                        spvalues['event_id'] = sp[1]
                    if sp[0] == 'dtm':
                        spvalues['dvce_created_tstamp'] = sp[1]
                    if sp[0] == 'vp':
                        ressplitter = re.compile(r'([^x]*)x*')
                        brdim = ressplitter.findall(sp[1])[:-1]
                        spvalues['br_viewwidth'] = brdim[1]
                        spvalues['br_viewheight'] = brdim[0]
                        continue
                    if sp[0] == 'ds':
                        ressplitter = re.compile(r'([^x]*)x*')
                        docdim = ressplitter.findall(sp[1])[:-1]
                        spvalues['doc_width'] = docdim[1]
                        spvalues['doc_height'] = docdim[0]
                        continue
                    if sp[0] == 'vid':
                        spvalues['domain_sessionidx'] = sp[1]
                    if sp[0] == 'sid':
                        spvalues['domain_sessionid'] = sp[1]
                    if sp[0] == 'duid':
                        spvalues['domain_userid'] = sp[1]
                    if sp[0] == 'fp':
                        spvalues['user_fingerprint'] = sp[1]
                    if sp[0] == 'ue_px':
                        spvalues['unstruct_event'] = sp[1]
                    if sp[0] == 'refr':
                        spvalues['page_referrer'] = unquote(unquote(sp[1]))
                    if sp[0] == 'tid':
                        spvalues['txn_id'] = sp[1]
                    if sp[0] == 'uid':
                        spvalues['user_id'] = sp[1]
                    if (sp[0] == 'nuid') or (sp[0] == 'tnuid'):
                        spvalues['network_userid'] = sp[1]
                    if sp[0] == 'se_ca':
                        spvalues['se_category'] = sp[1]
                    if sp[0] == 'se_ac':
                        spvalues['se_action'] = sp[1]
                    if sp[0] == 'se_la':
                        spvalues['se_label'] = sp[1]
                    if sp[0] == 'se_pr':
                        spvalues['se_property'] = sp[1]
                    if sp[0] == 'se_va':
                        spvalues['se_value'] = sp[1]
                    if sp[0] == 'tr_id':
                        spvalues['tr_orderid'] = sp[1]
                    if sp[0] == 'tr_af':
                        spvalues['tr_affiliation'] = sp[1]
                    if sp[0] == 'tr_tt':
                        spvalues['tr_total'] = sp[1]
                    if sp[0] == 'tr_tx':
                        spvalues['tr_tax'] = sp[1]
                    if sp[0] == 'tr_sh':
                        spvalues['tr_shipping'] = sp[1]
                    if sp[0] == 'tr_ci':
                        spvalues['tr_city'] = sp[1]
                    if sp[0] == 'tr_st':
                        spvalues['tr_state'] = sp[1]
                    if sp[0] == 'tr_co':
                        spvalues['tr_country'] = sp[1]
                    if sp[0] == 'ti_id':
                        spvalues['ti_orderid'] = sp[1]
                    if sp[0] == 'ti_sk':
                        spvalues['ti_sku'] = sp[1]
                    if sp[0] == 'ti_na':
                        spvalues['ti_name'] = sp[1]
                    if sp[0] == 'ti_ca':
                        spvalues['ti_category'] = sp[1]
                    if sp[0] == 'ti_pr':
                        spvalues['ti_price'] = sp[1]
                    if sp[0] == 'ti_qu':
                        spvalues['ti_quantity'] = sp[1]
                    if sp[0] == 'tr_cu':
                        spvalues['tr_currency'] = sp[1]
                    if sp[0] == 'ti_cu':
                        spvalues['ti_currency'] = sp[1]
                    if sp[0] == 'evn':
                        spvalues['event_vendor'] = sp[1]
                    if sp[0] == 'ue_pr':
                        spvalues['unstruct_event_unencoded'] = sp[1]
                    if sp[0] == 'cx':
                        spvalues['context'] = sp[1]
                #     new_line = ''
                #     for key,val in spvalues.items():
                #         new_line += str(val) + '\t'
                # datvalues += new_line + '\n'
                all_events.append(spvalues)
                
                i += 1

    except Exception as e:
        tprint(file_key, "Error: " + str(e))
        error_count += 1     

    tprint(file_key, "Processed " + str(i) + " entries")


    ########################
    #### Sorting events by destination and storing corresponding CSV files

    j = 0

    csvs = {} ## Dictionary to store all CSVs

    tprint(file_key, "Sorting events per destination and storing to CSV")

    for spvalues in all_events:
        try:
            unstruct_event_bool = False
            context_present = False
            custom_schema_str = ''
            j+=1
            for key,val in copy.deepcopy(spvalues).items():
                if val == '-' or val == ():
                    del spvalues[key]

            if 'dvce_created_tstamp' in spvalues:
                try:
                    spvalues['dvce_created_tstamp'] = datetime.datetime.fromtimestamp(int(spvalues['dvce_created_tstamp'])/1000).strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pass
            if 'dvce_sent_tstamp' in spvalues:
                try:
                    spvalues['dvce_sent_tstamp'] = datetime.datetime.fromtimestamp(int(spvalues['dvce_sent_tstamp'])/1000).strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pass
                    
            
            
            if 'unstruct_event' in spvalues:
                unstruct_event_bool = True
                # decode from base64 and parse into dictionary
                params = base64.urlsafe_b64decode(spvalues['unstruct_event'] + '===').decode("utf-8")
                unstruct_event = json.loads(params)
                del spvalues['unstruct_event']

            elif 'unstruct_event_unencoded' in spvalues:        
                # parse into dictionary
                unstruct_event_bool = True
                params = urllib.parse.unquote(urllib.parse.unquote(spvalues['unstruct_event_unencoded']))
                unstruct_event = json.loads(params)
                del spvalues['unstruct_event_unencoded']
                


            # assign context to a variable
            if 'context' in spvalues:
                context_present = True
                # decode from base64 and parse into dictionary
                context_decoded = base64.urlsafe_b64decode(spvalues['context'] + '===').decode("utf-8")
                context = json.loads(context_decoded)
                del spvalues['context']
                
            ## In any event we store an atomic.events entry
            columns_names = list(spvalues.keys())
            columns_names_str = ', '.join(columns_names)
            binds_str = ', '.join('%s' for _ in range(len(columns_names)))
            values = [spvalues[column_name]
                for column_name in columns_names]

            ### Generating CSV for the atomic event
            event_new_line = ''
            for column in table_columns['events']:
                if column in spvalues.keys():
                    event_new_line += str(spvalues[column]).replace("'", r"\'") + '\t'
                else:
                    event_new_line += '\t'
            event_new_line = re.sub('\t$', '\n', event_new_line)
            
            if "events" not in csvs.keys():
                csvs["events"] = ""
            csvs["events"] += event_new_line    


            if unstruct_event_bool:
                unstruct_event['data']['data']['root_id'] = spvalues['event_id']

                # define the corresponding schema name
                if re.search(r'achievement_gui_interaction',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_achievement_gui_interaction_1'
                if re.search(r'achievement_unlocked',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_achievement_unlocked_1'
                if re.search(r'email_click',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_email_click_1'
                if re.search(r'email_opened',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_email_opened_1'
                if re.search(r'email_sent',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_email_sent_1'
                if re.search(r'landing_from_email',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_landing_from_email_1'
                if re.search(r'user_creation',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_user_creation_1'
                if re.search(r'blockchain_account_creation',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_blockchain_account_creation_1'      
                if re.search(r'user_new_identity',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_user_new_identity_1'
                if re.search(r'link_click',unstruct_event['data']['schema']):
                    custom_schema_str = 'com_snowplowanalytics_snowplow_link_click_1'
                    # convert camel snake fields to snake case
                    for key in copy.deepcopy(unstruct_event['data']['data']).keys():
                        newKey = camel_to_snake(key)
                        unstruct_event['data']['data'][newKey] = unstruct_event['data']['data'].pop(key)
                if re.search(r'stream_watch',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_stream_watch_1'
                if re.search(r'new_creator_account',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_new_creator_account_1'
                if re.search(r'stream_session_started',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_stream_session_started_1'
                if re.search(r'stream_session_ended',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_stream_session_ended_1'
                if re.search(r'challenge_sent_to_live_channel',unstruct_event['data']['schema']):
                    custom_schema_str = 'io_azarus_challenge_sent_to_live_channel_1'

                if len(custom_schema_str) > 0:
                    unstruct_event_data = flatten(unstruct_event['data']['data'])
                    columns_names_custom = list(unstruct_event_data.keys())
                    columns_names_custom_str = ', '.join('"{0}"'.format(c) for c in columns_names_custom)
                    binds_custom_str = ', '.join('%s' for _ in range(len(columns_names_custom)))
                    values_custom = [unstruct_event_data[column_name_custom]
                        for column_name_custom in columns_names_custom]


                    ##### Generating a CSV file for the corresponding custom event
                    custom_event_new_line = ''
                    if custom_schema_str in table_columns.keys():
                        for column in table_columns[custom_schema_str]:
                            if column in unstruct_event_data.keys():
                                custom_event_new_line += str(unstruct_event_data[column]).replace("'", r"\'") + '\t'
                            else:
                                custom_event_new_line += '\t'
                        custom_event_new_line = re.sub('\t$', '\n', custom_event_new_line)

                        if custom_schema_str not in csvs.keys():
                            csvs[custom_schema_str] = ""
                        csvs[custom_schema_str] += custom_event_new_line
                    else:
                        tprint(file_key, "ERROR " + str(custom_schema_str) + " not in table columns")
            
                    
            # process context and prepare sql if custom context is found
            custom_cx_sqls = []
            if context_present:
                
                # iterate over all contexts and check for custom ones
                for cx in context['data']:
                    custom_cx_schema_str = ''
                    # define the corresponding custom context schema name
                    if re.search(r'twitch_user_context',cx['schema']):
                        custom_cx_schema_str = 'io_azarus_twitch_user_context_1'
                    
                    if len(custom_cx_schema_str) > 0:
                        cx['data']['root_id'] = spvalues['event_id']
                        custom_cx_data = flatten(cx['data'])
                        columns_names_custom_cx = list(custom_cx_data.keys())
                        columns_names_custom_cx_str = ', '.join('"{0}"'.format(c) for c in columns_names_custom_cx)
                        binds_cx_custom_str = ', '.join('%s' for _ in range(len(columns_names_custom_cx)))
                        values_custom_cx = [custom_cx_data[column_name_custom_cx]
                            for column_name_custom_cx in columns_names_custom_cx]                

                        ### Generating CSV for the corresponding context events
                        context_event_new_line = ''
                        if custom_cx_schema_str in table_columns.keys():
                            for column in table_columns[custom_cx_schema_str]:
                                if column in custom_cx_data.keys():
                                    context_event_new_line += str(custom_cx_data[column]).replace("'", r"\'") + '\t'
                                else:
                                    context_event_new_line += '\t'
                            context_event_new_line = re.sub('\t$', '\n', context_event_new_line)

                            if custom_cx_schema_str not in csvs.keys():
                                csvs[custom_cx_schema_str] = ""
                            csvs[custom_cx_schema_str] += context_event_new_line
                        else:
                            tprint(file_key, "ERROR " + str(custom_cx_schema_str) + " not in table columns")
        except Exception as e:
            tprint(file_key, "EventError. One event was not processed due to the following error: " + str(e))
            
    
    ########################
    #### Sorting events by destination and storing corresponding CSV files 

    conn = psycopg2.connect(host=os.environ['POSTGRES_HOST'], database=os.environ['POSTGRES_DATABASE'], user=os.environ['POSTGRES_USER'], password=os.environ['POSTGRES_PASSWORD'])


    ########################
    #### Inserting CSV contents to corresponding tables 
    tprint(file_key, "Inserting events into corresponding table")
    insertion_error = False
    if len(csvs.keys()):
        for k in csvs.keys():
            tprint(file_key, "Events " + str(k))
            inserts_file_name = "/tmp/" + k + "_data_" + str(hashlib.sha224(csvs[k].encode('utf-8')).hexdigest()) + '.tsv'
        
            try:
                f = open(inserts_file_name, "w")
                f.write(csvs[k])
                f.close()
            
                ### INSERTING
                cur = conn.cursor()
                cur.copy_from(open(inserts_file_name, 'r'), "atomic." + str(k), null='', sep='\t')
                
                
            except Exception as e:
                tprint(file_key, "Insertion failed for table " + str(k) + ". Error : " + str(e))
                error_count += 1
                insertion_error = True

        if not insertion_error:
            conn.commit()
        else:
            conn.rollback()

        cur.close()
        conn.close()


    ########################
    #### Dumping enriched CSV file to S3
    # tprint(file_key, "Writing enriched data to S3")
    # try: 
    #     if len(urispltnodes[0]) > 5:
    #         gz_body = BytesIO()
    #         gz = gzip.GzipFile(None, 'wb', 9, gz_body)
    #         gz.write(datvalues.encode('utf-8'))
    #         gz.close()
    #         s3.put_object(Bucket=bucket_name, Key=file_key.replace("RAW", "Converted"),  ContentType='text/plain',  ContentEncoding='gzip',  Body=gz_body.getvalue())
    # except Exception as e:
    #     tprint(file_key, "Error: " + str(e))
    #     error_count += 1

    ########################
    #### Writing log file to keep track of processed files

    if not insertion_error:
        tprint(file_key, "Writing log file to S3")
        try: 
            if len(urispltnodes[0]) > 5:
                gz_body = BytesIO()
                gz = gzip.GzipFile(None, 'wb', 9, gz_body)
                gz.write("".encode('utf-8'))
                gz.close()
                s3.put_object(Bucket=bucket_name, Key=file_key.replace("RAW", "Processed"),  ContentType='text/plain',  ContentEncoding='gzip',  Body=gz_body.getvalue())
        except Exception as e:
            tprint(file_key, "Error: " + str(e))
            error_count += 1    

        if error_count:
            tprint(file_key, "NbErrors: " + str(error_count))