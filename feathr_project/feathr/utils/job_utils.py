from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Union

from loguru import logger
import pandas as pd
from pyspark.sql import DataFrame, SparkSession

from feathr.client import FeathrClient
from feathr.constants import OUTPUT_FORMAT
from feathr.utils.platform import is_databricks


def get_result_pandas_df(
    client: FeathrClient,
    data_format: str = None,
    res_url: str = None,
    local_cache_path: str = None,
) -> pd.DataFrame:
    """Download the job result dataset from cloud as a Pandas DataFrame.

    Args:
        client: Feathr client
        data_format: Format to read the downloaded files. Currently support `parquet`, `delta`, `avro`, and `csv`.
            Default to use client's job tags if exists.
        res_url: Result URL to download files from. Note that this will not block the job so you need to make sure
            the job is finished and the result URL contains actual data. Default to use client's job tags if exists.
        local_cache_path (optional): Specify the absolute download path. if the user does not provide this,
            the function will create a temporary directory.

    Returns:
        pandas DataFrame
    """
    return get_result_df(client, data_format, res_url, local_cache_path)


def get_result_spark_df(
    spark: SparkSession,
    client: FeathrClient,
    data_format: str = None,
    res_url: str = None,
    local_cache_path: str = None,
) -> DataFrame:
    """Download the job result dataset from cloud as a Spark DataFrame.

    Args:
        spark: Spark session
        client: Feathr client
        data_format: Format to read the downloaded files. Currently support `parquet`, `delta`, `avro`, and `csv`.
            Default to use client's job tags if exists.
        res_url: Result URL to download files from. Note that this will not block the job so you need to make sure
            the job is finished and the result URL contains actual data. Default to use client's job tags if exists.
        local_cache_path (optional): Specify the absolute download path. if the user does not provide this,
            the function will create a temporary directory.

    Returns:
        Spark DataFrame
    """
    return get_result_df(client, data_format, res_url, local_cache_path, spark=spark)


def get_result_df(
    client: FeathrClient,
    data_format: str = None,
    res_url: str = None,
    local_cache_path: str = None,
    spark: SparkSession = None,
) -> Union[DataFrame, pd.DataFrame]:
    """Download the job result dataset from cloud as a Spark DataFrame or pandas DataFrame.

    Args:
        client: Feathr client
        data_format: Format to read the downloaded files. Currently support `parquet`, `delta`, `avro`, and `csv`.
            Default to use client's job tags if exists.
        res_url: Result URL to download files from. Note that this will not block the job so you need to make sure
            the job is finished and the result URL contains actual data. Default to use client's job tags if exists.
        local_cache_path (optional): Specify the absolute download directory. if the user does not provide this,
            the function will create a temporary directory.
        spark (optional): Spark session. If provided, the function returns spark Dataframe.
            Otherwise, it returns pd.DataFrame.

    Returns:
        Either Spark or pandas DataFrame.
    """
    if data_format is None:
        # May use data format from the job tags
        if client.get_job_tags() and client.get_job_tags().get(OUTPUT_FORMAT):
            data_format = client.get_job_tags().get(OUTPUT_FORMAT)
        else:
            raise ValueError("Cannot determine the data format. Please provide the data_format argument.")

    data_format = data_format.lower()

    if is_databricks() and client.spark_runtime != "databricks":
        raise RuntimeError(f"The function is called from Databricks but the client.spark_runtime is {client.spark_runtime}.")

    # TODO Loading Synapse Delta table result into pandas has a bug: https://github.com/delta-io/delta-rs/issues/582
    if not spark and client.spark_runtime == "azure_synapse" and data_format == "delta":
        raise RuntimeError(f"Loading Delta table result from Azure Synapse into pandas DataFrame is not supported. You maybe able to use spark DataFrame to load the result instead.")

    # use a result url if it's provided by the user, otherwise use the one provided by the job
    res_url: str = res_url or client.get_job_result_uri(block=True, timeout_sec=1200)
    if res_url is None:
        raise ValueError(
            "`res_url` is None. Please make sure either you provide a res_url or make sure the job finished in FeathrClient has a valid result URI."
        )

    if client.spark_runtime == "local":
        if local_cache_path is not None:
            logger.warning(
                "In local spark mode, the result files are expected to be stored at a local storage and thus `local_cache_path` argument will be ignored."
            )
        local_cache_path = res_url

    elif client.spark_runtime == "databricks":
        if not res_url.startswith("dbfs:"):
            logger.warning(
                f"In Databricks, the result files are expected to be stored in DBFS, but the res_url {res_url} is not a dbfs path. Prefixing it with 'dbfs:/'"
            )
            res_url = f"dbfs:/{res_url.lstrip('/')}"

        if is_databricks():  # Check if the function is being called from Databricks
            if local_cache_path is not None:
                logger.warning(
                    "Result files are already in DBFS and thus `local_cache_path` will be ignored."
                )
            local_cache_path = res_url

    if local_cache_path is None:
        local_cache_path = TemporaryDirectory().name

    if local_cache_path != res_url:
        logger.info(f"{res_url} files will be downloaded into {local_cache_path}")
        client.feathr_spark_launcher.download_result(result_path=res_url, local_folder=local_cache_path)

    result_df = None
    try:
        if spark is not None:
            if data_format == "csv":
                result_df = spark.read.option("header", True).csv(local_cache_path)
            else:
                result_df = spark.read.format(data_format).load(local_cache_path)
        else:
            result_df = _load_files_to_pandas_df(
                dir_path=local_cache_path.replace("dbfs:", "/dbfs"),  # replace to python path if spark path is provided.
                data_format=data_format,
            )
    except Exception as e:
        logger.error(f"Failed to load result files from {local_cache_path} with format {data_format}.")
        raise e

    return result_df


def _load_files_to_pandas_df(dir_path: str, data_format: str = "avro") -> pd.DataFrame:

    if data_format == "parquet":
        return pd.read_parquet(dir_path)

    elif data_format == "delta":
        from deltalake import DeltaTable
        delta = DeltaTable(dir_path)
        return delta.to_pyarrow_table().to_pandas()

    elif data_format == "avro":
        import pandavro as pdx
        if Path(dir_path).is_file():
            return pdx.read_avro(dir_path)
        else:
            try:
                return pd.concat([pdx.read_avro(f) for f in Path(dir_path).glob("*.avro")]).reset_index(drop=True)
            except ValueError:  # No object to concat when the dir is empty
                return pd.DataFrame()

    elif data_format == "csv":
        if Path(dir_path).is_file():
            return pd.read_csv(dir_path)
        else:
            try:
                return pd.concat([pd.read_csv(f) for f in Path(dir_path).glob("*.csv")]).reset_index(drop=True)
            except ValueError:  # No object to concat when the dir is empty
                return pd.DataFrame()

    else:
        raise ValueError(
            f"{data_format} is currently not supported in get_result_df. Currently only parquet, delta, avro, and csv are supported, please consider writing a customized function to read the result."
        )
