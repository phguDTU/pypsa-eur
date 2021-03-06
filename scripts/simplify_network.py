# coding: utf-8

import pandas as pd
idx = pd.IndexSlice

import logging
logger = logging.getLogger(__name__)

import os
import re
import numpy as np
import scipy as sp
from scipy.sparse.csgraph import connected_components
import xarray as xr
import geopandas as gpd
import shapely
import networkx as nx

from six import iteritems
from six.moves import reduce

import pypsa
from pypsa.io import import_components_from_dataframe, import_series_from_dataframe
from pypsa.networkclustering import (busmap_by_stubs, busmap_by_kmeans,
                                     _make_consense, get_clustering_from_busmap,
                                     aggregategenerators, aggregateoneport)

from cluster_network import clustering_for_n_clusters, cluster_regions

def simplify_network_to_380(n):
    ## All goes to v_nom == 380
    logger.info("Mapping all network lines onto a single 380kV layer")

    n.buses['v_nom'] = 380.

    linetype_380, = n.lines.loc[n.lines.v_nom == 380., 'type'].unique()
    lines_v_nom_b = n.lines.v_nom != 380.
    n.lines.loc[lines_v_nom_b, 'num_parallel'] *= (n.lines.loc[lines_v_nom_b, 'v_nom'] / 380.)**2
    n.lines.loc[lines_v_nom_b, 'v_nom'] = 380.
    n.lines.loc[lines_v_nom_b, 'type'] = linetype_380
    n.lines.loc[lines_v_nom_b, 's_nom'] = (
        np.sqrt(3) * n.lines['type'].map(n.line_types.i_nom) *
        n.lines.bus0.map(n.buses.v_nom) * n.lines.num_parallel
    )

    # Replace transformers by lines
    trafo_map = pd.Series(n.transformers.bus1.values, index=n.transformers.bus0.values)
    trafo_map = trafo_map[~trafo_map.index.duplicated(keep='first')]
    several_trafo_b = trafo_map.isin(trafo_map.index)
    trafo_map.loc[several_trafo_b] = trafo_map.loc[several_trafo_b].map(trafo_map)
    missing_buses_i = n.buses.index.difference(trafo_map.index)
    trafo_map = trafo_map.append(pd.Series(missing_buses_i, missing_buses_i))

    for c in n.one_port_components|n.branch_components:
        df = n.df(c)
        for col in df.columns:
            if col.startswith('bus'):
                df[col] = df[col].map(trafo_map)

    n.mremove("Transformer", n.transformers.index)
    n.mremove("Bus", n.buses.index.difference(trafo_map))

    return n, trafo_map

def _aggregate_and_move_components(n, busmap, aggregate_one_ports={"Load", "StorageUnit"}):
    def replace_components(n, c, df, pnl):
        n.mremove(c, n.df(c).index)

        import_components_from_dataframe(n, df, c)
        for attr, df in iteritems(pnl):
            if not df.empty:
                import_series_from_dataframe(n, df, c, attr)

    generators, generators_pnl = aggregategenerators(n, busmap)
    replace_components(n, "Generator", generators, generators_pnl)

    for one_port in aggregate_one_ports:
        df, pnl = aggregateoneport(n, busmap, component=one_port)
        replace_components(n, one_port, df, pnl)

    buses_to_del = n.buses.index.difference(busmap)
    n.mremove("Bus", buses_to_del)
    for c in n.branch_components:
        df = n.df(c)
        n.mremove(c, df.index[df.bus0.isin(buses_to_del) | df.bus1.isin(buses_to_del)])

def simplify_links(n):
    ## Complex multi-node links are folded into end-points
    logger.info("Simplifying connected link components")

    # Determine connected link components, ignore all links but DC
    adjacency_matrix = n.adjacency_matrix(branch_components=['Link'],
                                          weights=dict(Link=(n.links.carrier == 'DC').astype(float)))

    _, labels = connected_components(adjacency_matrix, directed=False)
    labels = pd.Series(labels, n.buses.index)

    G = n.graph()

    def split_links(nodes):
        nodes = frozenset(nodes)

        seen = set()
        supernodes = {m for m in nodes
                      if len(G.adj[m]) > 2 or (set(G.adj[m]) - nodes)}

        for u in supernodes:
            for m, ls in iteritems(G.adj[u]):
                if m not in nodes or m in seen: continue

                buses = [u, m]
                links = [list(ls)] #[name for name in ls]]

                while m not in (supernodes | seen):
                    seen.add(m)
                    for m2, ls in iteritems(G.adj[m]):
                        if m2 in seen or m2 == u: continue
                        buses.append(m2)
                        links.append(list(ls)) # [name for name in ls])
                        break
                    else:
                        # stub
                        break
                    m = m2
                if m != u:
                    yield pd.Index((u, m)), buses, links
            seen.add(u)

    busmap = n.buses.index.to_series()

    for lbl in labels.value_counts().loc[lambda s: s > 2].index:

        for b, buses, links in split_links(labels.index[labels == lbl]):
            if len(buses) <= 2: continue

            logger.debug('nodes = {}'.format(labels.index[labels == lbl]))
            logger.debug('b = {}\nbuses = {}\nlinks = {}'.format(b, buses, links))

            m = sp.spatial.distance_matrix(n.buses.loc[b, ['x', 'y']],
                                           n.buses.loc[buses[1:-1], ['x', 'y']])
            busmap.loc[buses] = b[np.r_[0, m.argmin(axis=0), 1]]
            all_links = [i for _, i in sum(links, [])]

            p_max_pu = snakemake.config['links'].get('p_max_pu', 1.)
            lengths = n.links.loc[all_links, 'length']
            name = lengths.idxmax() + '+{}'.format(len(links) - 1)
            params = dict(
                carrier='DC',
                bus0=b[0], bus1=b[1],
                length=sum(n.links.loc[[i for _, i in l], 'length'].mean() for l in links),
                p_nom=min(n.links.loc[[i for _, i in l], 'p_nom'].sum() for l in links),
                underwater_fraction=sum(lengths/lengths.sum() * n.links.loc[all_links, 'underwater_fraction']),
                p_max_pu=p_max_pu,
                p_min_pu=-p_max_pu,
                underground=False,
                under_construction=False
            )

            logger.info("Joining the links {} connecting the buses {} to simple link {}".format(", ".join(all_links), ", ".join(buses), name))

            n.mremove("Link", all_links)

            static_attrs = n.components["Link"]["attrs"].loc[lambda df: df.static]
            for attr, default in static_attrs.default.iteritems(): params.setdefault(attr, default)
            n.links.loc[name] = pd.Series(params)

            # n.add("Link", **params)

    logger.debug("Collecting all components using the busmap")

    _aggregate_and_move_components(n, busmap)
    return n, busmap

def remove_stubs(n):
    logger.info("Removing stubs")

    busmap = busmap_by_stubs(n, ['country'])
    _aggregate_and_move_components(n, busmap)

    return n, busmap


if __name__ == "__main__":
    # Detect running outside of snakemake and mock snakemake for testing
    if 'snakemake' not in globals():
        from vresutils.snakemake import MockSnakemake, Dict
        snakemake = MockSnakemake(
            path='..',
            wildcards=Dict(simpl=''),
            input=Dict(
                network='networks/elec.nc',
                regions_onshore='resources/regions_onshore.geojson',
                regions_offshore='resources/regions_offshore.geojson'
            ),
            output=Dict(
                network='networks/elec_s{simpl}.nc',
                regions_onshore='resources/regions_onshore_s{simpl}.geojson',
                regions_offshore='resources/regions_offshore_s{simpl}.geojson'
            )
        )

    logger = logging.getLogger()
    logger.setLevel(snakemake.config['logging_level'])

    n = pypsa.Network(snakemake.input.network)

    n, trafo_map = simplify_network_to_380(n)

    n, simplify_links_map = simplify_links(n)

    n, stub_map = remove_stubs(n)

    busmaps = [trafo_map, simplify_links_map, stub_map]

    if snakemake.wildcards.simpl:
        n_clusters = int(snakemake.wildcards.simpl)
        clustering = clustering_for_n_clusters(n, n_clusters)

        n = clustering.network
        busmaps.append(clustering.busmap)

    n.export_to_netcdf(snakemake.output.network)

    busemap_s = reduce(lambda x, y: x.map(y), busmaps[1:], busmaps[0])
    with pd.HDFStore(snakemake.output.clustermaps, model='w') as store:
        store.put('busmap_s', busemap_s, format="table", index=False)

    cluster_regions(busmaps, snakemake.input, snakemake.output)
