from datetime import datetime, timedelta

import pandas as pd
import prefect
from legal_api.models import Filing, Business
from prefect import task, Flow, unmapped
from prefect.executors import LocalDaskExecutor
from prefect.schedules import IntervalSchedule

from config import get_named_config
from common.firm_queries import get_unprocessed_firms_query
from common.event_filing_service import EventFilingService, REGISTRATION_EVENT_FILINGS
from common.firm_filing_data_cleaning_utils import clean_naics_data, clean_corp_party_data, clean_offices_data
from common.processing_status_service import ProcessingStatusService
from custom_filer.filer import process_filing
from common.custom_exceptions import CustomException
from common.lear_data_utils import populate_filing_json_from_lear
from common.firm_filing_json_factory_service import FirmFilingJsonFactoryService
from common.firm_filing_data_utils import get_is_paper_only
from tasks.task_utils import ColinInitTask, LearInitTask
from sqlalchemy import engine, text


colin_init_task = ColinInitTask(name='init_colin')
lear_init_task = LearInitTask(name='init_lear', flask_app_name='lear-test-etl', nout=2)


@task
def get_config():
    config = get_named_config()
    return config


@task(name='get_unprocessed_firms')
def get_unprocessed_firms(config, db_engine: engine):
    logger = prefect.context.get("logger")

    query = get_unprocessed_firms_query(config.DATA_LOAD_ENV)
    sql_text = text(query)

    with db_engine.connect() as conn:
        rs = conn.execute(sql_text)
        df = pd.DataFrame(rs, columns=rs.keys())
        raw_data_dict = df.to_dict('records')
        corp_nums = [x.get('corp_num') for x in raw_data_dict]
        logger.info(f'{len(raw_data_dict)} corp_nums to process from colin data: {corp_nums}')

    return raw_data_dict


@task(name='get_event_filing_data')
def get_event_filing_data(config, colin_db_engine: engine, unprocessed_firm_dict: dict):
    logger = prefect.context.get("logger")
    status_service = ProcessingStatusService(config.DATA_LOAD_ENV, colin_db_engine)
    event_filing_service = EventFilingService(colin_db_engine)
    corp_num = unprocessed_firm_dict.get('corp_num')
    corp_name = ''
    print(f'get event filing data for {corp_num}')

    event_ids = unprocessed_firm_dict.get('event_ids')
    event_file_types = unprocessed_firm_dict.get('event_file_types')
    event_file_types = event_file_types.split(',')
    unprocessed_firm_dict['event_file_types'] = event_file_types
    event_filing_data_arr = []

    prev_event_filing_data = None
    for idx, event_id in enumerate(event_ids):
        event_file_type = event_file_types[idx]
        is_supported_event_filing = event_filing_service.get_event_filing_is_supported(event_file_type)
        print(f'event_id: {event_id}, event_file_type: {event_file_type}, is_supported_event_filing: {is_supported_event_filing}')
        if is_supported_event_filing:
            prev_event_ids = event_ids[:idx]
            event_filing_data_dict = \
                event_filing_service.get_event_filing_data(corp_num, event_id,
                                                           event_file_type,
                                                           prev_event_filing_data,
                                                           prev_event_ids)
            event_filing_data_arr.append({
                'processed': True,
                'data': event_filing_data_dict
            })
            prev_event_filing_data = event_filing_data_dict
        else:
            # event_filing_data_arr.append({
            #     'processed': False,
            #     'data': None
            # })
            error_msg = f'not processing this firm as there is an unsupported event/filing type: {event_file_type}'
            status_service.update_flow_status(flow_name='sp-gp-flow',
                                              corp_num=corp_num,
                                              corp_name=corp_name,
                                              processed_status='FAILED',
                                              failed_event_id=event_id,
                                              last_error=error_msg)
            raise CustomException(f'not processing this firm as there is an unsupported event/filing type: {event_file_type}')

    unprocessed_firm_dict['event_filing_data'] = event_filing_data_arr
    return unprocessed_firm_dict


@task(name='clean_event_filing_data')
def clean_event_filing_data(config, colin_db_engine: engine, event_filing_data_dict: dict):
    logger = prefect.context.get("logger")
    status_service = ProcessingStatusService(config.DATA_LOAD_ENV, colin_db_engine)
    corp_num = event_filing_data_dict['corp_num']
    corp_name = ''
    event_id = None
    event_filing_type = None

    try:
        event_filing_data_arr = event_filing_data_dict['event_filing_data']
        for event_filing_data in event_filing_data_arr:
            if event_filing_data['processed']:
                filing_data = event_filing_data['data']
                event_filing_type = filing_data['event_file_type']
                event_id=filing_data['e_event_id']
                corp_name = filing_data['curr_corp_name']
                clean_naics_data(filing_data)
                clean_corp_party_data(filing_data)
                clean_offices_data(filing_data)
    except Exception as err:
        error_msg = f'error cleaning business {corp_num}, {corp_name}, {err}'
        logger.error(error_msg)
        status_service.update_flow_status(flow_name='sp-gp-flow',
                                          corp_num=corp_num,
                                          corp_name=corp_name,
                                          processed_status='FAILED',
                                          failed_event_id=event_id,
                                          failed_event_file_type=event_filing_type,
                                          last_error=error_msg)
        raise CustomException(error_msg, event_filing_data_dict)

    return event_filing_data_dict


@task(name='transform_event_filing_data')
def transform_event_filing_data(config, app: any, colin_db_engine: engine, db_lear, event_filing_data_dict: dict):
    logger = prefect.context.get("logger")
    status_service = ProcessingStatusService(config.DATA_LOAD_ENV, colin_db_engine)
    corp_num = event_filing_data_dict['corp_num']
    corp_name = ''
    event_id = None
    event_filing_type = None

    try:
        with app.app_context():
            event_filing_data_arr = event_filing_data_dict['event_filing_data']
            for event_filing_data in event_filing_data_arr:
                if event_filing_data['processed']:
                    # process and create LEAR json filing dict
                    filing_data = event_filing_data['data']
                    event_filing_type = filing_data['event_file_type']
                    event_id=filing_data['e_event_id']
                    corp_name = filing_data['curr_corp_name']
                    firm_filing_json_factory_service = FirmFilingJsonFactoryService(event_filing_data)
                    filing_json = firm_filing_json_factory_service.get_filing_json()
                    event_filing_data['filing_json'] = filing_json
    except Exception as err:
        error_msg = f'error transforming business {corp_num}, {corp_name}, {err}'
        logger.error(error_msg)
        status_service.update_flow_status(flow_name='sp-gp-flow',
                                          corp_num=corp_num,
                                          corp_name=corp_name,
                                          processed_status='FAILED',
                                          failed_event_id=event_id,
                                          failed_event_file_type=event_filing_type,
                                          last_error=error_msg)
        raise CustomException(error_msg, event_filing_data_dict)

    return event_filing_data_dict


@task(name='load_event_filing_data')
def load_event_filing_data(config, app: any, colin_db_engine: engine, db_lear, event_filing_data_dict: dict):
    logger = prefect.context.get("logger")
    status_service = ProcessingStatusService(config.DATA_LOAD_ENV, colin_db_engine)
    corp_num = event_filing_data_dict['corp_num']
    corp_name = ''
    event_id = None
    event_filing_type = None

    try:
        with app.app_context():
            event_filing_data_arr = event_filing_data_dict['event_filing_data']
            for event_filing_data in event_filing_data_arr:
                if event_filing_data['processed']:
                    business = None
                    filing_data = event_filing_data['data']
                    event_id=filing_data['e_event_id']
                    event_filing_type = filing_data['event_file_type']
                    if not REGISTRATION_EVENT_FILINGS.has_value(event_filing_type):
                        business = Business.find_by_identifier(corp_num)
                    target_lear_filing_type = filing_data['target_lear_filing_type']
                    filing_json = event_filing_data['filing_json']
                    populate_filing_json_from_lear(db_lear, event_filing_data, business)
                    effective_date = filing_data['f_effective_dts']
                    corp_name = filing_data['curr_corp_name']

                    # save filing to filing table
                    filing = Filing()
                    filing.effective_date = effective_date
                    filing._filing_json = filing_json
                    filing._filing_type = target_lear_filing_type
                    filing.filing_date = effective_date
                    filing.business_id = business.id if business else None
                    filing.source = Filing.Source.COLIN.value
                    filing.paper_only = get_is_paper_only(filing_data)
                    db_lear.session.add(filing)
                    db_lear.session.commit()

                    # process filing with custom filer function
                    process_filing(filing.id, filing_data, db_lear)
                    status_service.update_flow_status(flow_name='sp-gp-flow',
                                                      corp_num=corp_num,
                                                      corp_name=corp_name,
                                                      processed_status='COMPLETED',
                                                      last_processed_event_id=event_id)

                    # confirm can access from dashboard if we use existing account for now
    except Exception as err:
        error_msg = f'error loading business {corp_num}, {corp_name}, {err}'
        logger.error(error_msg)
        status_service.update_flow_status(flow_name='sp-gp-flow',
                                          corp_num=corp_num,
                                          corp_name=corp_name,
                                          processed_status='FAILED',
                                          failed_event_id=event_id,
                                          failed_event_file_type=event_filing_type,
                                          last_error=error_msg)
        db_lear.session.rollback()


# now = datetime.utcnow()
# schedule = IntervalSchedule(interval=timedelta(minutes=10), start_date=now)
#
# with Flow("SP-GP-Migrate-ETL", schedule=schedule, executor=LocalDaskExecutor(scheduler="threads")) as f:

with Flow("SP-GP-Migrate-ETL", executor=LocalDaskExecutor(scheduler="threads") ) as f:

    # setup
    config = get_config()
    db_colin_engine = colin_init_task(config)
    FLASK_APP, db_lear = lear_init_task(config)

    unprocessed_firms = get_unprocessed_firms(config, db_colin_engine)

    # get event/filing related data for each firm
    event_filing_data = get_event_filing_data.map(unmapped(config),
                                                  colin_db_engine=unmapped(db_colin_engine),
                                                  unprocessed_firm_dict=unprocessed_firms)

    # clean/validate filings for a given business
    cleaned_event_filing_data = clean_event_filing_data.map(unmapped(config),
                                                            unmapped(db_colin_engine),
                                                            event_filing_data)

    # transform data to appropriate format in preparation for data loading into LEAR
    transformed_event_filing_data = transform_event_filing_data.map(unmapped(config),
                                                                    unmapped(FLASK_APP),
                                                                    unmapped(db_colin_engine),
                                                                    unmapped(db_lear),
                                                                    cleaned_event_filing_data)

    # load all filings for a given business sequentially
    # if a filing fails, flag business as failed indicating which filing it failed at
    loaded_event_filing_data = load_event_filing_data.map(unmapped(config),
                                                          unmapped(FLASK_APP),
                                                          unmapped(db_colin_engine),
                                                          unmapped(db_lear),
                                                          transformed_event_filing_data)

result = f.run()