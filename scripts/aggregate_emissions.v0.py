import json

import dask
import fsspec
import numcodecs
import numpy as np
import regionmask
import xarray as xr

TC02_PER_TC = 3.67
TC_PER_TBM = 0.5
SQM_PER_HA = 10000
ORNL_SCALING = 0.1
R = 6.371e6

# @joe - will this damage it if i do this globally? maybe this shouldn't all be in the same script hmmm
# dask.config.set(**{"array.slicing.split_large_chunks": False})


HANSEN_FILE_LIST = (
    "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2018-v1.6/treecover2000.txt"
)
OUT_TILE_TEMPLATE = "gs://carbonplan-climatetrace/v0/tiles/{}_{}.zarr"
OUT_RASTER_FILE = "gs://carbonplan-climatetrace/v0/3000m_raster.zarr"
OUT_COUNTRY_ROLLUP_FILE = "gs://carbonplan-climatetrace/v0/country_rollups.json"

LATS_TO_RUN = [
    "80N",
    "70N",
    "60N",
    "50N",
    "40N",
    "30N",
    "20N",
    "10N",
    "00N",
    "10S",
    "20S",
    "30S",
    "40S",
    "50S",
]
LONS_TO_RUN = [f"{n:03}W" for n in np.arange(10, 190, 10)] + [
    f"{n:03}E" for n in np.arange(0, 190, 10)
]

COARSENING_FACTOR = 100


def compute_grid_area(da):

    """
    Compute the geographic area (in square meters) of every pixel in the data array provided.

    Parameters
    ----------
    da : xarray data array
        Data array with spatial coordinates lon and lat

    Returns
    -------
    areacella / SQM_PER_HA : data array
        Data array with grid cell areas in square meters
    """

    dϕ = np.radians((da["lat"][1] - da["lat"][0]).values)
    dλ = np.radians((da["lon"][1] - da["lon"][0]).values)
    dA = R ** 2 * np.abs(dϕ * dλ) * np.cos(np.radians(da["lat"]))
    areacella = dA * (0 * da + 1)

    return areacella / SQM_PER_HA


def create_coarsened_global_raster():
    with fsspec.open(HANSEN_FILE_LIST) as f:
        lines = f.read().decode().splitlines()
    print("We are working with {} different files".format(len(lines)))

    # the arrays where you'll throw your active lat/lon permutations
    lats = []
    lons = []

    encoding = {"emissions": {"compressor": numcodecs.Blosc()}}

    for line in lines:
        pieces = line.split("_")
        lat = pieces[-2]
        lon = pieces[-1].split(".")[0]

        if (lat in LATS_TO_RUN) and (lon in LONS_TO_RUN):
            lats.append(lat)
            lons.append(lon)
    all_to_do = len(lats)
    done = 0
    list_all_coarsened = []
    for lat, lon in list(zip(lats, lons)):

        try:
            # We only have data over land so this will throw
            # an exception if the tile errors (likely for lack of data - could be improved to check
            # that it fails precisely because it is an ocean tile - aka we check that all of the land
            # cells run appropriately)
            mapper = fsspec.get_mapper(OUT_TILE_TEMPLATE.format(lat, lon))
            da_global = xr.open_zarr(mapper, consolidated=True)
            # We only want to create the
            da_mask = da_global.isel(year=0, drop=True)
            da_area = compute_grid_area(da_mask)
            list_all_coarsened.append(
                (da_global * da_area)
                .coarsen(lat=COARSENING_FACTOR, lon=COARSENING_FACTOR)
                .sum()
                .compute(retries=4)
            )
        except ValueError:
            print("{} {} did not work (likely because it is ocean) booooo".format(lat, lon))
        done += 1
        print("completed {} of {} tiles".format(done, all_to_do))
    coarsened_url = OUT_RASTER_FILE

    mapper = fsspec.get_mapper(coarsened_url)

    combined_ds = xr.combine_by_coords(list_all_coarsened, compat="override", coords="minimal")
    combined_ds = combined_ds.chunk({"lat": -1, "lon": -1, "year": 1})
    task = combined_ds.to_zarr(mapper, encoding=encoding, mode="w", compute=False)
    dask.compute(task, retries=4)


def package_country_rollup_json(data):

    """
    packages your dictionary nicely for writing out to json
    """

    out = {}
    for k, d in data.items():
        timseries = []
        for y, v in d.items():
            timseries.append({"year": y, "emissions": v})
        out[k] = timseries
    return out


def country_rollups(input_raster, out_file):
    mapper = fsspec.get_mapper(input_raster)
    ds = xr.open_zarr(mapper)

    # access your country boundaries via the boundaries in regionmask. these political boundaries
    countries = regionmask.defined_regions.natural_earth.countries_110
    names = dict(zip(countries.numbers, countries.names))
    mask = countries.mask(ds.emissions)

    # aggregate each country's emissions, summing up the total emissions at every component gridcell
    # and produce a country-wide estimate for the period of record
    df = ds.emissions.groupby(mask).sum().to_pandas()
    columns = {k: names[int(k)] for k in df.columns}
    df = df.rename(columns=columns)

    # convert to teragrams
    data = (df / 1e9).to_dict()

    # package it nicely
    out = package_country_rollup_json(data)

    with fsspec.open(out_file, "w") as f:
        json.dump(out, f, indent=2)


def main():
    # first create the coarsened global raster - this will be at a coarsened resolution but will cover
    # the globe in regularly-spaced lat/lon grid
    from dask.distributed import Client

    client = Client(n_workers=8, threads_per_worker=1)
    print(client.dashboard_link)
    # then you can append this ^^ to your jupyterhub link to access the dask dashboard
    create_coarsened_global_raster()

    # then create the country roll-ups which provide country-specific averages
    # we might want to do this at the 30 m resolution eventually, but for now it's fine
    # to do at the 3 km resolution

    country_rollups(input_raster=OUT_RASTER_FILE, out_file=OUT_COUNTRY_ROLLUP_FILE)


if __name__ == "__main__":
    main()
