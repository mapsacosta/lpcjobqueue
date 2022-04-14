"""A dask-jobqueue plugin for the LPC Condor queue
   Connecting to an existing Dask Gateway Kubecluster
   Running on Kubernetes
"""
from .cluster import LPCGatewayCluster, LPCGatewayCondorJob
from .version import version as __version__

__all__ = ["__version__", "LPCGatewayCondorJob", "LPCGatewayCluster"]
