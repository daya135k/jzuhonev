import astropy.io.fits as pyfits
import numpy as np
from soxs.utils import parse_prng, parse_value, \
    ensure_numpy_array, mylog
from soxs.spatial import construct_wcs
from astropy.units import Quantity
from collections import Sequence
import os


def _parse_catalog_entry(src):
    import re
    brackets = re.findall(r"[^[]*\[([^]]*)\]", src)
    src_file = src.split("[")[0]
    ext = brackets[0].lower().split(",")
    extname = ext[0]
    if len(ext) == 2:
        extver = int(ext[1])
    else:
        extver = None
    if len(brackets) == 2 and extname == "spectrum":
        rowstr = brackets[1].lower()
        row = rowstr.split("==")[1].strip("'")
        if "row" in rowstr:
            row = int(row)
    else:
        row = None
    return src_file, extname, extver, row


class LazyReadSimputCatalog(Sequence):
    def __init__(self, src_cat):
        self.src_cat = src_cat

    def __getitem__(self, i):
        spec = self.src_cat.spectra[i]
        return self.src_cat.read_source(spec)

    def __len__(self):
        return self.src_cat.num_sources


def read_simput_catalog(simput_file):
    r"""
    Read events from a SIMPUT catalog. This will read 
    all of the sources in the catalog.

    Parameters
    ----------
    simput_file : string
        The SIMPUT file to read from.

    Returns
    -------
    1. Lists of dicts of NumPy arrays of the positions 
       and energies of the events from the sources.
    2. NumPy arrays of the parameters of the sources.
    """
    sc = SimputCatalog.from_file(simput_file)
    parameters = {"emin": sc.emin,
                  "emax": sc.emax,
                  "src_names": sc.src_names,
                  "flux": sc.fluxes}
    return LazyReadSimputCatalog(sc), parameters


class SimputCatalog:

    def __init__(self, spectra, images, src_names, ra, dec, fluxes,
                 emin, emax, filename):
        self.spectra = ensure_numpy_array(spectra)
        self.images = ensure_numpy_array(images)
        self.src_names = ensure_numpy_array(src_names)
        self.ra = ensure_numpy_array(ra)
        self.dec = ensure_numpy_array(dec)
        self.fluxes = ensure_numpy_array(fluxes)
        self.emin = ensure_numpy_array(emin)
        self.emax = ensure_numpy_array(emax)
        self.filename = filename
        self.num_sources = self.spectra.size
        # timing not yet supported
        self.timing = np.array(["NULL"]*self.num_sources)

    @classmethod
    def from_source(cls, filename, source, src_filename=None,
                    overwrite=False):
        """
        Create a new :class:`~soxs.simput.SimputCatalog`
        instance using a single :class:`~soxs.simput.SimputSource`
        instance.

        Parameters
        ----------
        filename : string
            The name of the SIMPUT catalog file to write. The
            file will be written 
        source : :class:`~soxs.simput.SimputSource`
            The SIMPUT source to create the catalog with.
        src_filename : string, optional
            If set, this will be the filename to write the source
            to. By default, the source will be written to the same
            file as the SIMPUT catalog.
        overwrite : boolean, optional
            Whether or not to overwrite an existing file with
            the same name. If src_filename=None and the source is
            to the written to the SIMPUT catalog file, then this
            argument is ignored. If src_filename is another value,
            it exists, and overwrite=False, the source will be
            appended to the file. Default: False

        """
        sc = cls([], [], [], [], [], [], [], [], filename)
        sc._write_catalog(overwrite=overwrite)
        sc.append(source, src_filename=src_filename,
                  overwrite=overwrite)
        return sc

    @classmethod
    def from_file(cls, filename):
        """
        Generate a SIMPUT catalog object by reading it in from
        disk.

        Parameters
        ----------
        filename : string
            The name of the SIMPUT catalog file to read the
            catalog and photon lists from.
        """
        f_simput = pyfits.open(filename)
        fluxes = f_simput["src_cat"].data["flux"]
        src_names = f_simput["src_cat"].data["src_name"]
        e_min = f_simput["src_cat"].data["e_min"]
        e_max = f_simput["src_cat"].data["e_max"]
        ra = f_simput["src_cat"].data["ra"]
        dec = f_simput["src_cat"].data["dec"]
        spectra = f_simput["src_cat"].data["spectrum"]
        if "IMAGE" in f_simput["src_cat"].columns.names:
            images = f_simput["src_cat"].data["image"]
        else:
            images = ["NULL"]*len(spectra)
        f_simput.close()

        return cls(spectra, images, src_names, ra, dec, fluxes, e_min, e_max,
                   filename)

    def read_source(self, spec):
        """
        Read a source from the SIMPUT catalog with the identifier
        *spec* for the SPECTRUM field.
        """
        from .spectra import Spectrum
        from astropy.io.fits.column import _VLF
        i = np.where(self.spectra == spec)[0][0]
        src_file, extname, extver, row = _parse_catalog_entry(spec)
        # If no file is specified, assume the catalog and
        # source are in the same file
        if src_file == "":
            fn_src = self.filename
        else:
            fn_src = src_file
        ext = extname if extver is None else (extname, extver)
        data = pyfits.getdata(fn_src, ext)
        if extname == "phlist":
            ra = Quantity(data["ra"], "deg")
            dec = Quantity(data["dec"], "deg")
            energy = Quantity(data["energy"], "keV")
            src = SimputPhotonList(ra, dec, energy, self.fluxes[i],
                                   name=self.src_names[i])
        elif extname == "spectrum":
            emid = data["energy"]
            flux = data["fluxdensity"]
            if isinstance(data["energy"], _VLF):
                emid = emid[0]
                flux = flux[0]
            elif row is not None:
                if isinstance(row, str):
                    row = np.where(data["name"])[0][0]
                emid = emid[row, :]
                flux = flux[row, :]
            de = np.diff(emid)[0]
            ebins = np.append(emid - 0.5 * de, emid[-1] + 0.5 * de)
            spec = Spectrum(ebins, flux)
            if self.images[i].upper() != "NULL":
                src_file, extname, extver, _ = \
                    _parse_catalog_entry(self.images[i])
                # If no file is specified, assume the catalog and
                # source are in the same file
                if src_file == "":
                    fn_img = self.filename
                else:
                    fn_img = src_file
                ext = extname if extver is None else (extname, extver)
                imdata, imheader = pyfits.getdata(
                    fn_img, ext, header=True)
                imhdu = pyfits.ImageHDU(data=imdata,
                                        header=imheader)
            else:
                imhdu = None
            src = SimputSpectrum(spec, self.ra[i], self.dec[i],
                                 name=self.src_names[i], imhdu=imhdu)
        else:
            raise RuntimeError

        return src

    def _write_catalog(self, overwrite=False):
        """
        Write the SIMPUT catalog to disk.
        """
        src_id = np.arange(self.num_sources)
        col1 = pyfits.Column(name='SRC_ID', format='J', array=src_id)
        col2 = pyfits.Column(name='RA', format='D', array=self.ra)
        col3 = pyfits.Column(name='DEC', format='D', array=self.dec)
        col4 = pyfits.Column(name='E_MIN', format='D', array=self.emin)
        col5 = pyfits.Column(name='E_MAX', format='D', array=self.emax)
        col6 = pyfits.Column(name='FLUX', format='D', array=self.fluxes)
        col7 = pyfits.Column(name='SPECTRUM', format='80A', array=self.spectra)
        col8 = pyfits.Column(name='IMAGE', format='80A', array=self.images)
        col9 = pyfits.Column(name='TIMING', format='80A', array=self.timing)
        col10 = pyfits.Column(name='SRC_NAME', format='80A',
                              array=self.src_names)

        coldefs = pyfits.ColDefs([col1, col2, col3, col4, col5,
                                  col6, col7, col8, col9, col10])

        wrhdu = pyfits.BinTableHDU.from_columns(coldefs)
        wrhdu.name = "SRC_CAT"

        wrhdu.header["HDUCLASS"] = "HEASARC"
        wrhdu.header["HDUCLAS1"] = "SIMPUT"
        wrhdu.header["HDUCLAS2"] = "SRC_CAT"
        wrhdu.header["HDUVERS"] = "1.1.0"
        wrhdu.header["RADECSYS"] = "FK5"
        wrhdu.header["EQUINOX"] = 2000.0
        wrhdu.header["TUNIT2"] = "deg"
        wrhdu.header["TUNIT3"] = "deg"
        wrhdu.header["TUNIT4"] = "keV"
        wrhdu.header["TUNIT5"] = "keV"
        wrhdu.header["TUNIT6"] = "erg/s/cm**2"

        if os.path.exists(self.filename) and not overwrite:
            with pyfits.open(self.filename, mode='update') as f:
                f["SRC_CAT"] = wrhdu
                f.flush()
        else:
            f = [pyfits.PrimaryHDU(), wrhdu]
            pyfits.HDUList(f).writeto(self.filename, overwrite=True)

    def append(self, source, src_filename=None, overwrite=False):
        """
        Add a source to this catalog.

        Parameters
        ----------
        source : :class:`~soxs.simput.SimputSource`
            The SIMPUT source to append to this catalog.
        src_filename : string, optional
            If set, this will be the filename to write the source
            to. By default, the source will be written to the same
            file as the SIMPUT catalog.
        overwrite : boolean, optional
            Whether or not to overwrite an existing file with
            the same name. If src_filename=None and the source is
            to the written to the SIMPUT catalog file, then this
            argument is ignored. If src_filename is another value,
            it exists, and overwrite=False, the source will be
            appended to the file. Default: False
        """
        self.src_names = np.append(self.src_names, source.name)
        self.ra = np.append(self.ra, source.ra)
        self.dec = np.append(self.dec, source.dec)
        self.fluxes = np.append(self.fluxes, source.flux)
        self.emin = np.append(self.emin, source.emin)
        self.emax = np.append(self.emax, source.emax)
        self.num_sources += 1

        if src_filename is None:
            src_filename = self.filename

        if src_filename == self.filename:
            # Don't overwrite the SIMPUT catalog file!!
            overwrite = False

        extver = _determine_extver(src_filename, source.src_type.upper())
        spec_fn = src_filename if src_filename != self.filename else ""
        spec = f"{spec_fn}[{source.src_type.upper()},{extver}]"
        if source.imhdu is not None:
            img_extver = _determine_extver(src_filename, "IMAGE")
            img_fn = src_filename if src_filename != self.filename else ""
            img = f"{img_fn}[IMAGE,{extver}]"
        else:
            img = "NULL"
            img_extver = None
        self.spectra = np.append(self.spectra, spec)
        self.images = np.append(self.images, img)

        self._write_catalog()
        source._write_source(src_filename, extver, img_extver=img_extver,
                             overwrite=overwrite)


def _determine_extver(fn, extname):
    extver = 1
    if os.path.exists(fn):
        with pyfits.open(fn) as f:
            for hdu in f:
                if hdu.name == extname:
                    extver = hdu.header["EXTVER"]+1
    return extver


class SimputSource:
    src_type = "null"

    def __init__(self, emin, emax, flux, ra, dec, name=None, imhdu=None):
        self.emin = emin
        self.emax = emax
        if name is None:
            name = ""
        self.name = name
        self.flux = flux
        self.ra = ra
        self.dec = dec
        self.imhdu = imhdu

    def _get_source_hdu(self):
        return None, None

    def _write_source(self, filename, extver, img_extver=None, overwrite=False):
        coldefs, header = self._get_source_hdu()

        tbhdu = pyfits.BinTableHDU.from_columns(coldefs)
        tbhdu.name = self.src_type.upper()

        if self.src_type == "phlist":
            hduclas1 = "PHOTONS"
        else:
            hduclas1 = self.src_type.upper()
        tbhdu.header["HDUCLASS"] = "HEASARC/SIMPUT"
        tbhdu.header["HDUCLAS1"] = hduclas1
        tbhdu.header["HDUVERS"] = "1.1.0"
        tbhdu.header.update(header)

        tbhdu.header["EXTVER"] = extver
        if self.imhdu is not None:
            self.imhdu.header["EXTVER"] = img_extver

        if os.path.exists(filename) and not overwrite:
            mylog.info(f"Appending this source to {filename}.")
            with pyfits.open(filename, mode='append') as f:
                f.append(tbhdu)
                if self.imhdu is not None:
                    f.append(self.imhdu)
                f.flush()
        else:
            if os.path.exists(filename):
                mylog.warning(f"Overwriting {filename} with this source.")
            else:
                mylog.info(f"Writing source to {filename}.")
            f = [pyfits.PrimaryHDU(), tbhdu]
            if self.imhdu is not None:
                f.append(self.imhdu)
            pyfits.HDUList(f).writeto(filename, overwrite=overwrite)

        if self.imhdu is not None:
            self.imhdu.header["EXTVER"] = 1


class SimputSpectrum(SimputSource):
    src_type = "spectrum"

    def __init__(self, spec, ra, dec, name=None, imhdu=None):
        super(SimputSpectrum, self).__init__(spec.ebins.value.min(),
                                             spec.ebins.value.max(),
                                             spec.total_flux.value, ra,
                                             dec, name=name, imhdu=imhdu)
        self.spec = spec

    def _get_source_hdu(self):
        col1 = pyfits.Column(name='ENERGY', format='E',
                             array=self.spec.emid.value)
        col2 = pyfits.Column(name='FLUXDENSITY', format='D',
                             array=self.spec.flux.value)
        cols = [col1, col2]

        coldefs = pyfits.ColDefs(cols)

        header = {"REFRA": self.ra,
                  "REFDEC": self.dec,
                  "TUNIT1": "keV",
                  "TUNIT2": "photon/(cm**2*s*keV)"}

        return coldefs, header

    @classmethod
    def from_spectrum(cls, name, spectral_model, ra, dec):
        """
        Generates a SIMPUT spectrum model for a point source
        from a spectral model and a coordinate on the sky.

        Parameters
        ----------
        name : string
            The name of the SIMPUT spectrum.
        spectral_model : :class:`~soxs.spectra.Spectrum`
            The spectral model to use to generate the event energies.
        ra : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The RA of the source in degrees.
        dec : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The Dec of the source in degrees.
        """
        return cls(spectral_model, ra, dec, name=name)

    @classmethod
    def from_models(cls, name, spectral_model, spatial_model,
                    width, nx):
        """
        Generate a SIMPUT spectrum from a spectral and a spatial
        model, and parameters for an image.

        Parameters
        ----------
        name : string
            The name of the SIMPUT spectrum.
        spectral_model : :class:`~soxs.spectra.Spectrum`
            The spectral model to use to generate the event energies.
        spatial_model : :class:`~soxs.spatial.SpatialModel`
            The spatial model to use to generate the event coordinates.
        width : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The width of the image in arcminutes.
        nx : integer
            The resolution of the image, e.g. the number of pixels
            on a side.
        """
        imhdu = spatial_model.generate_image(width, nx)
        return cls(spectral_model, spatial_model.ra0,
                   spatial_model.dec0, name=name, imhdu=imhdu)


class SimputPhotonList(SimputSource):
    src_type = "phlist"

    def __init__(self, ra, dec, energy, flux, name=None):
        emin = np.asarray(energy).min()
        emax = np.asarray(energy).max()
        super(SimputPhotonList, self).__init__(
            emin, emax, flux, 0.0, 0.0, name=name)
        self.events = {"ra": ra, "dec": dec, "energy": energy}
        self.num_events = energy.size

    def __getitem__(self, item):
        return self.events[item]

    def __contains__(self, item):
        return item in self.events

    def __iter__(self):
        for key in self.events:
            yield key

    @classmethod
    def from_models(cls, name, spectral_model, spatial_model,
                    t_exp, area, prng=None):
        """
        Generate a SIMPUT photon list from a spectral and a spatial
        model. 

        Parameters
        ----------
        name : string
            The name of the SIMPUT photon list.
        spectral_model : :class:`~soxs.spectra.Spectrum`
            The spectral model to use to generate the event energies.
        spatial_model : :class:`~soxs.spatial.SpatialModel`
            The spatial model to use to generate the event coordinates.
        t_exp : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The exposure time in seconds.
        area : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The effective area in cm**2. If one is creating 
            events for a SIMPUT file, a constant should be 
            used and it must be large enough so that a 
            sufficiently large sample is drawn for the ARF.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time. 
        """
        prng = parse_prng(prng)
        t_exp = parse_value(t_exp, "s")
        area = parse_value(area, "cm**2")
        e = spectral_model.generate_energies(t_exp, area, prng=prng)
        ra, dec = spatial_model.generate_coords(e.size, prng=prng)
        return cls(ra, dec, e, e.flux.value, name=name)

    def _get_source_hdu(self):
        col1 = pyfits.Column(name='ENERGY', format='E', 
                             array=self["energy"].value)
        col2 = pyfits.Column(name='RA', format='D', array=self["ra"].value)
        col3 = pyfits.Column(name='DEC', format='D', array=self["dec"].value)
        cols = [col1, col2, col3]

        coldefs = pyfits.ColDefs(cols)

        header = {"REFRA": 0.0,
                  "REFDEC": 0.0,
                  "TUNIT1": "keV",
                  "TUNIT2": "deg",
                  "TUNIT3": "deg"}

        return coldefs, header

    def plot(self, center, width, s=None, c=None, marker=None, stride=1,
             emin=None, emax=None, label=None, fontsize=18, fig=None, 
             ax=None, **kwargs):
        """
        Plot event coordinates from this photon list in a scatter plot, 
        optionally restricting the photon energies which are plotted
        and using only a subset of the photons. 

        Parameters
        ----------
        center : array-like
            The RA, Dec of the center of the plot in degrees.
        width : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The width of the plot in arcminutes.
        s : integer, optional
            Size of the scatter marker in points^2.
        c : string, optional
            The color of the points.
        marker : string, optional
            The marker to use for the points in the scatter plot. Default: 'o'
        stride : integer, optional
            Plot every *stride* events. Default: 1
        emin : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The minimum energy of the photons to plot. Default is
            the minimum energy in the list.
        emax : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
            The maximum energy of the photons to plot. Default is
            the maximum energy in the list.
        label : string, optional
            The label of the spectrum. Default: None
        fontsize : int
            Font size for labels and axes. Default: 18
        fig : :class:`~matplotlib.figure.Figure`, optional
            A Figure instance to plot in. Default: None, one will be
            created if not provided.
        ax : :class:`~matplotlib.axes.Axes`, optional
            An Axes instance to plot in. Default: None, one will be
            created if not provided.
        """
        import matplotlib.pyplot as plt
        from astropy.visualization.wcsaxes import WCSAxes
        if fig is None:
            fig = plt.figure(figsize=(10, 10))
        if ax is None:
            wcs = construct_wcs(center[0], center[1])
            ax = WCSAxes(fig, [0.15, 0.1, 0.8, 0.8], wcs=wcs)
            fig.add_axes(ax)
        else:
            wcs = ax.wcs
        if emin is None:
            emin = self.emin
        else:
            emin = parse_value(emin, "keV")
        if emax is None:
            emax = self.emax
        else:
            emax = parse_value(emax, "keV")
        idxs = np.logical_and(self["energy"].value >= emin, self["energy"].value <= emax)
        ra = self["ra"][idxs][::stride].value
        dec = self["dec"][idxs][::stride].value
        x, y = wcs.wcs_world2pix(ra, dec, 1)
        ax.scatter(x, y, s=s, c=c, marker=marker, label=label, **kwargs)
        x0, y0 = wcs.wcs_world2pix(center[0], center[1], 1)
        width = parse_value(width, "arcmin")*60.0
        ax.set_xlim(x0-0.5*width, x0+0.5*width)
        ax.set_ylim(y0-0.5*width, y0+0.5*width)
        ax.set_xlabel("RA")
        ax.set_ylabel("Dec")
        ax.tick_params(axis='both', labelsize=fontsize)
        return fig, ax
