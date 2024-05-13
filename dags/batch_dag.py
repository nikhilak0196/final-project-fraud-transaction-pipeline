import os
import logging

from airflow import DAG
from airflow.utils.dates import days_ago
from airflow.operators.empty import EmptyOperator
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.providers.google.cloud.transfers.local_to_gcs import LocalFilesystemToGCSOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryCreateExternalTableOperator, BigQueryCreateEmptyDatasetOperator, BigQueryDeleteTableOperator

import pyarrow.csv as pv
import pyarrow.parquet as pq

from datetime import datetime
from datetime import timedelta

from google.cloud import storage

path_to_local_home = "/opt/airflow"
# dataset_file = "PS_20174392719_1491204439457_log.csv"
date_str = datetime.now().strftime('%Y%m%d%H%M%S')
parquet_file = f"online_transaction.parquet"
parquet_file_transform = f"online_transaction_transform-{date_str}.parquet"
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
BUCKET = os.environ.get("GCP_GCS_BUCKET")
BIGQUERY_DATASET = os.environ.get("BIGQUERY_DATASET", 'onlinetransaction_wh')
STG_TABLE = 'stg_onlinepayment'

# def format_to_parquet(ti):
#     filename = ti.xcom_pull(task_ids='download_dataset')
#     if not filename.endswith('.csv'):
#         logging.error("Can only accept source files in CSV format, for the moment")
#         return
#     table = pv.read_csv(f"{path_to_local_home}/datasets/{filename}")
#     return pq.write_table(table, f"{path_to_local_home}/datasets/{parquet_file}")

def upload_to_gcs(bucket, object_name, local_file):
    """
    Ref: https://cloud.google.com/storage/docs/uploading-objects#storage-upload-object-python
    :param bucket: GCS bucket name
    :param object_name: target path & file-name
    :param local_file: source path & file-name
    :return:
    """
    # WORKAROUND to prevent timeout for files > 6 MB on 800 kbps upload speed.
    # (Ref: https://github.com/googleapis/python-storage/issues/74)
    storage.blob._MAX_MULTIPART_SIZE = 5 * 1024 * 1024  # 5 MB
    storage.blob._DEFAULT_CHUNKSIZE = 5 * 1024 * 1024  # 5 MB
    # End of Workaround

    client = storage.Client()
    bucket = client.bucket(bucket)

    blob = bucket.blob(object_name)
    blob.upload_from_filename(local_file, 
                            #   timeout=300,
                              )

default_args = {
    "owner": "airflow",
    "start_date": days_ago(1),
    "retries": 1,
    "retry_delay": timedelta(minutes=5)
}

with DAG(
    dag_id="batch_workflow",
    default_args=default_args,
    schedule_interval="@monthly"
) as dag:
    start = EmptyOperator(
        task_id="start"
    )

    download_dataset = BashOperator(
        task_id="download_dataset",
        bash_command="download.sh",
        # xcom_push = True
    )

    # format_to_parquet_task = PythonOperator(
    #     task_id="format_to_parquet_task",
    #     python_callable=format_to_parquet,
    #     # op_kwargs={
    #     #     "src_file": f"{path_to_local_home}/datasets/{dataset_file}",
    #     # },
    # )

    spark_data_transformation = BashOperator(
        task_id="spark_data_transformation",
        bash_command=f"cd /opt/airflow/spark && python3 spark_transform.py {{{{ti.xcom_pull(task_ids='download_dataset')}}}} {parquet_file_transform} && ls /opt/airflow/datasets/{parquet_file_transform}/*.parquet",
        # xcom_push = True
    )

    # upload_to_gcs = LocalFilesystemToGCSOperator(
    #     task_id="upload_to_gcs",
    #     src=f"{{{{ti.xcom_pull(task_ids='spark_data_transformation')}}}}",
    #     dst=f"datasets/{parquet_file}",
    #     bucket=BUCKET,
    # )

    upload_to_gcs = PythonOperator(
        task_id="upload_to_gcs",
        python_callable=upload_to_gcs,
        op_kwargs={
            "bucket": BUCKET,
            "object_name": f"datasets/online_transaction_{{{{ dag_run.get_task_instance('start').start_date }}}}.parquet",                      
            "local_file": f"{{{{ti.xcom_pull(task_ids='spark_data_transformation')}}}}",   
        },
    )

    bigquery_external_table_task = BigQueryCreateExternalTableOperator(
        task_id="bigquery_external_table_task",
        table_resource={
            "tableReference": {
                "projectId": PROJECT_ID,
                "datasetId": BIGQUERY_DATASET,
                "tableId": "external_table",
            },
            "externalDataConfiguration": {
                "sourceFormat": "PARQUET",
                "sourceUris": [f"gs://{BUCKET}/datasets/online_transaction_{{{{ dag_run.get_task_instance('start').start_date }}}}.parquet"],
                "autodetect": True
            },
        },
    )

    initiate_staging_task = BashOperator(
        task_id = "initiate_staging_task",
        bash_command = f"cd /opt/airflow/dbt && dbt deps && dbt run --select {STG_TABLE} --profiles-dir . --target prod"
    )

    transform_task = BashOperator(
        task_id = "transform_task",
        bash_command = f"cd /opt/airflow/dbt && dbt deps && dbt run --exclude {STG_TABLE} --profiles-dir . --target prod"
    )

    delete_staging_table_task = BigQueryDeleteTableOperator(
        task_id = "delete_staging_table_task",
        deletion_dataset_table = f'{PROJECT_ID}.{BIGQUERY_DATASET}.{STG_TABLE}',
        ignore_if_missing = True
    )

    # bq_partition_clustering = EmptyOperator(
    #     task_id="bq_partition_clustering"
    # )

    end = EmptyOperator(
        task_id="end"
    )

    start >> download_dataset >> spark_data_transformation >>\
    upload_to_gcs >> bigquery_external_table_task >>\
    initiate_staging_task >> transform_task >> delete_staging_table_task >> end

