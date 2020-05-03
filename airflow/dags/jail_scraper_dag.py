# standard library
import datetime as dt
from datetime import datetime
import os
import math
import csv
import logging

# external dependencies
from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.models import Variable

# custom packages
from csci_utils.airflow import PythonIdempatomicFileOperator, requires
from jail_scraper.airflow_scraper import main
from odyssey_scraper.smartsearch import SmartSearchScraper

SCRAPE_ROOT = "data/scrapes/" + datetime.today().strftime("%m-%d-%Y") + "/"

def scrape_jail(output_path, test):
    main(scrape_dir=output_path, test = test)
    return output_path

def check_jail_profiles(output_path, **kwargs):

    # load filepaths from required task
    reqs = requires('scrape_jail', **kwargs)
    logging.info('Requirements:', str(reqs))

    # get info for people in jail (which is stored in people.csv in dir created by 'scrape_jail')
    with open(reqs['people'], "r", newline="") as fout:
        data = list(csv.reader(fout))
    logging.info("opened people.csv")

    # user decides scrapes_per_worker depending on personal preference and number of scrapers
    scrapes_per_worker = int(Variable.get("scrapes_per_worker", default_var=3))
    logging.info("scrapes_per_worker = " + str(scrapes_per_worker))

    # how many people need their profiles scraped
    num_people_to_scrape = len(data)
    logging.info("num_people_to_scrape = " + str(num_people_to_scrape))

    # this determines how many tasks we create to do the scraping (which allows it to be done in
    # parallel when deployed)
    num_tasks = math.ceil(num_people_to_scrape/scrapes_per_worker)
    logging.info("num_tasks = " + str(num_tasks))

    # split big list of people into 'to do lists" for each of the workers. If worker fails, it can
    # be re-run and pull in exact same people
    for x in range(num_tasks):
        chunk = data[x*scrapes_per_worker+1:(x+1)*scrapes_per_worker+1]
        out_path = os.path.join(output_path, f"todo_{x}.csv")
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(chunk)
        logging.info(f"wrote todo_{x}")

    # set variable in Airflow (stored in meta-db) to use in constructing dynamic DAG later
    Variable.set('num_odyssey_scraping_tasks', str(num_tasks))
    return 'Complete'

def scrape_odyssey(index, output_path, **kwargs):

    logging.info(f'running scraper {index}')

    out_path = output_path
    reqs = requires('check_profiles', **kwargs)

    # read in "to do list"
    with open(reqs[f'todo_{index}'], "r", newline="") as fout:
        data = list(csv.reader(fout))

    # log in information for Odyssey Criminal Justice Portal (Shelby County, TN)
    odyssey_user = os.environ['ODYSSEY_USER']
    odyssey_pwd = os.environ['ODYSSEY_PASS']
    # scraper that I developed for this system
    scr = SmartSearchScraper(odyssey_user, odyssey_pwd)

    total_cases = []
    for person in data:
        logging.info(str(person))
        name = person[1]
        dob = person[2]
        justice_history = scr.query_name_dob(name, dob, get_rni=True)
        for case in justice_history.case_grid_list():
            # combine all a person's cases into a list of lists (instead of a list of list of lists)
            total_cases += case
    scr.quit()

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(total_cases)

    return 'Complete'


def upload_data(**kwargs):
    # dummy task for "uploading data"
    pass

default_args = {
    'owner': 'airflow',
    'start_date': dt.datetime(2020, 4, 30, 16, 00, 00),
    'concurrency': 1,
    'retries': 0
}

dag =  DAG('jail_scraper_dag',
         default_args=default_args,
         schedule_interval='@once',
         )


jail_scraper = PythonIdempatomicFileOperator(task_id='scrape_jail',
                                        python_callable=scrape_jail,
                                        output_pattern = SCRAPE_ROOT + "jail_scrape/",
                                        op_kwargs={'test':True},
                                        dag = dag)

check_profiles = PythonIdempatomicFileOperator(task_id='check_profiles',
                                            python_callable=check_jail_profiles,
                                            output_pattern = SCRAPE_ROOT + "to_do/",
                                            provide_context=True,
                                            dag = dag)
upload = PythonOperator(task_id='upload_data',
                        python_callable=upload_data,
                        dag=dag)

num_tasks = int(Variable.get("num_odyssey_scraping_tasks", default_var=1))
for i in range(num_tasks):
    odyssey_scraper = PythonIdempatomicFileOperator(task_id='odyssey_scraper_'+str(i),
                                                    output_pattern = SCRAPE_ROOT + "worker_{index}/cases.csv",
                                                    dag = dag,
                                                    python_callable=scrape_odyssey,
                                                    provide_context=True,
                                                    op_kwargs={'index': i})

    check_profiles.set_downstream(odyssey_scraper)
    odyssey_scraper.set_downstream(upload)




jail_scraper >> check_profiles

