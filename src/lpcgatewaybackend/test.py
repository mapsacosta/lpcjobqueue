from distributed import Client
from .cluster import LPCGatewayCluster 

cluster = LPCGatewayCluster()
#cluster.adapt(minimum=0, maximum=10)
#client = Client(cluster)
cluster
