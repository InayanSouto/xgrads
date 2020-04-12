# -*- coding: utf-8 -*-
"""
Created on 2020.04.11

@author: MiniUFO
Copyright 2018. All rights reserved. Use is subject to license terms.
"""
import os
import numpy as np
import xarray as xr
import dask.array as dsa
from .core import CtlDescriptor
from functools import reduce

"""
IO related functions here
"""
def open_CtlDataset(desfile, returnctl=False):
    """
    Open a 4D dataset with a descriptor file end with .ctl and
    return a xarray.Dataset.  This also uses the dask to chunk
    very large dataset, which is similar to the xarray.open_dataset.
    
    Parameters
    ----------
    desfile: string
        Path to the descriptor file end with .ctl or .cts
    returnctl: bool
        Return dset and ctl as a tuple
    
    Returns
    -------
    dset : xarray.Dataset
        Dataset object containing all coordinates and variables.
    ctl : xgrads.CtlDescriptor
        Ctl descriptor file.
    """

    if not desfile.endswith('.ctl'):
        raise Exception('unsupported file, suffix should be .ctl')

    ctl = CtlDescriptor(file=desfile)
    
    if ctl.template:
        tcount = len(ctl.tdef.samples) # number of total time count
        tcPerf = []                    # number of time count per file
        
        for file in ctl.dsetPath:
            fsize = os.path.getsize(file)
            
            if fsize % ctl.tRecLength != 0:
                raise Exception('incomplete file for ' + file +
                                '(not multiple of ' + ctl.tRecLength +
                                ' bytes)')
            
            tcPerf.append(fsize // ctl.tRecLength)
        
        total_size = sum(tcPerf)
        
        if total_size < tcount:
            raise Exception('no enough files for ' + str(tcount) +
                            ' time records')
        
        # get time record number in each file
        rem = tcount
        idx = 0
        for i, num in enumerate(tcPerf):
            rem -= num
            if rem <= 0:
                idx = i
                break

        tcPerf_m      = tcPerf[:idx+1]
        tcPerf_m[idx] = tcPerf[idx   ] + rem
        
        # print(ctl.dsetPath)
        # print(tcPerf)
        # print(tcPerf_m)
        
        binData = __read_template_as_dask(ctl, tcPerf_m)
        
    else:
        expect = ctl.tRecLength * ctl.tdef.length()
        actual = os.path.getsize(ctl.dsetPath)
        
        if expect != actual:
            print('WARNING: expected binary file size: {0}, actual size: {1}'
                            .format(expect, actual))
    
        binData = __read_as_dask(ctl)

    variables = []
    
    if ctl.pdef is None:
        for m, v in enumerate(ctl.vdef):
            if v.dependZ:
                da = xr.DataArray(name=v.name, data=binData[m],
                                  dims=['time', 'lev', 'lat', 'lon'],
                                  coords={'time': ctl.tdef.samples[:],
                                          'lev' : ctl.zdef.samples[:v.zcount],
                                          'lat' : ctl.ydef.samples[:],
                                          'lon' : ctl.xdef.samples[:]},
                                  attrs={'comment': v.comment,
                                         'storage': v.storage})
            else:
                t, z, y, x = binData[m].shape
                da = xr.DataArray(name=v.name,
                                  data=binData[m].reshape((t,y,x)),
                                  dims=['time', 'lat', 'lon'],
                                  coords={'time': ctl.tdef.samples[:],
                                          'lat' : ctl.ydef.samples[:],
                                          'lon' : ctl.xdef.samples[:]},
                                  attrs={'comment': v.comment,
                                         'storage': v.storage})
            variables.append(da)

    else:
        PDEF = ctl.pdef
        
        if PDEF.proj in ['lcc', 'lccr']:
            ycoord = np.linspace(0, (PDEF.jsize-1) * PDEF.dx, PDEF.jsize)
            xcoord = np.linspace(0, (PDEF.isize-1) * PDEF.dy, PDEF.isize)
            
        elif PDEF.proj in ['sps', 'nps']:
            inc = PDEF.gridinc * 1000 # change unit from km to m
            ycoord = np.linspace(-(PDEF.jpole), (PDEF.jsize-PDEF.jpole),
                                   PDEF.jsize) * inc
            xcoord = np.linspace(-(PDEF.ipole), (PDEF.isize-PDEF.ipole),
                                   PDEF.isize) * inc
        
        for m, v in enumerate(ctl.vdef):
            if v.dependZ:
                da = xr.DataArray(name=v.name, data=binData[m],
                                  dims=['time', 'lev', 'y', 'x'],
                                  coords={'time': ctl.tdef.samples[:],
                                          'lev' : ctl.zdef.samples[:v.zcount],
                                          'y' : ycoord,
                                          'x' : xcoord},
                                  attrs={'comment': v.comment,
                                         'storage': v.storage})
            else:
                t, z, y, x = binData[m].shape
                da = xr.DataArray(name=v.name,
                                  data=binData[m].reshape((t,y,x)),
                                  dims=['time', 'y', 'x'],
                                  coords={'time': ctl.tdef.samples[:],
                                          'y' : ycoord,
                                          'x' : xcoord},
                                  attrs={'comment': v.comment,
                                         'storage': v.storage})

            variables.append(da)

#    variables = {v.name: (['time','lev','lat','lon'], binData[m])
#                 for m,v in enumerate(ctl.vdef)}

    dset = xr.merge(variables)

    dset.attrs['title'] = ctl.title
    dset.attrs['undef'] = ctl.undef
    
    if returnctl:
        return dset, ctl
    else:
        return dset



"""
Helper (private) methods are defined below
"""
def __read_as_dask(dd):
    """
    Read binary data and return as a dask array
    """
    if dd.pdef is None:
        t, y, x = dd.tdef.length(), dd.ydef.length(), dd.xdef.length()
    else:
        t, y, x = dd.tdef.length(), dd.pdef.jsize, dd.pdef.isize

    totalNum = sum([reduce(lambda x, y:
                    x*y, (t,v.zcount,y,x)) for v in dd.vdef])

    # print(totalNum * 4.0 / 1024.0 / 1024.0)

    binData = []
    
    dtype   = '<f4' if dd.byteOrder == 'little' else '>f4'

    for m, v in enumerate(dd.vdef):
        if totalNum < (100 * 100 * 100 * 10): # about 40 MB, chunk all
            # print('small')
            chunk = (t, v.zcount, y, x)
            shape = (t, v.zcount, y, x)

            dsk = {(v.name+'_@miniufo', 0, 0, 0, 0):
                   (__read_var, dd.dsetPath, v, dd.tRecLength, None, None, dtype)}

            binData.append(dsa.Array(dsk, v.name+'_@miniufo', chunk,
                                     dtype=dtype,
                                     shape=shape))

        elif totalNum > (200 * 100 * 100 * 100): # about 800 MB, chunk 2D slice
            # print('large')
            chunk = (1, 1, y, x)
            shape = (t, v.zcount, y, x)

            dsk = {(v.name+'_@miniufo', l, k, 0, 0):
                   (__read_var, dd.dsetPath, v, dd.tRecLength, l, k, dtype)
                   for l in range(t)
                   for k in range(v.zcount)}

            binData.append(dsa.Array(dsk, v.name+'_@miniufo', chunk,
                                     dtype=dtype,
                                     shape=shape))

        else: # in between, chunk 3D slice
            # print('between')
            chunk = (1, v.zcount, y, x)
            shape = (t, v.zcount, y, x)

            dsk = {(v.name+'_@miniufo', l, 0, 0, 0):
                   (__read_var, dd.dsetPath, v, dd.tRecLength, l, None, dtype)
                   for l in range(t)}

            binData.append(dsa.Array(dsk, v.name+'_@miniufo', chunk,
                                     dtype=dtype,
                                     shape=shape))

    return binData


def __read_template_as_dask(dd, tcPerf):
    """
    Read template binary data and return as a dask array
    """
    t, y, x = dd.tdef.length(), dd.ydef.length(), dd.xdef.length()

    totalNum = sum([reduce(lambda x, y:
                    x*y, (tcPerf[0],v.zcount,y,x)) for v in dd.vdef])

    # print(totalNum * 4.0 / 1024.0 / 1024.0)

    binData = []
    
    dtype   = '<f4' if dd.byteOrder == 'little' else '>f4'

    for m, v in enumerate(dd.vdef):
        if totalNum > (200 * 100 * 100 * 100): # about 800 MB, chunk 2D slice
            # print('large')
            chunk = (1, 1, y, x)
            shape = (t, v.zcount, y, x)

            dsk = {(v.name+'_@miniufo', l + sum(tcPerf[:m]), k, 0, 0):
                   (__read_var, f, v, dd.tRecLength, l, k, dtype)
                   for m, f in enumerate(dd.dsetPath[:len(tcPerf)])
                   for l in range(tcPerf[m])
                   for k in range(v.zcount)}

            binData.append(dsa.Array(dsk, v.name+'_@miniufo', chunk,
                                     dtype=dtype,
                                     shape=shape))

        else: # in between, chunk 3D slice
            # print('between')
            chunk = (1, v.zcount, y, x)
            shape = (t, v.zcount, y, x)

            dsk = {(v.name+'_@miniufo', l + sum(tcPerf[:m]), 0, 0, 0):
                   (__read_var, f, v, dd.tRecLength, l, None, dtype)
                   for m, f in enumerate(dd.dsetPath[:len(tcPerf)])
                   for l in range(tcPerf[m])}

            binData.append(dsa.Array(dsk, v.name+'_@miniufo', chunk,
                                     dtype=dtype,
                                     shape=shape))

    return binData


def __read_var(file, var, tstride, tstep, zstep, dtype):
    """
    Read a variable given the trange.

    Parameters
    ----------
    file : str
        A file from which data are read.
    var  : CtlVar
        A variable that need to be read.
    tstride : int
        Stride of a single time record.
    tstep : int
        T-step to be read, started from 0.  If None, read all t-steps
    zstep : int
        Z-step to be read, started from 0.  If None, read all z-steps
    """
    # print(var.name+' '+str(tstep)+' '+str(zstep)+' '+str(var.strPos))

    if var.storage == '-1,20':
        if tstep is None and zstep is None:
            shape = (var.tcount, var.zcount, var.ycount, var.xcount)
            pos   = var.strPos
            return __read_continuous(file, pos, shape, dtype)
        
        elif zstep is None and tstep is not None:
            shape = (1, var.zcount, var.ycount, var.xcount)
            pos   = var.strPos
            return __read_continuous(file, pos, shape, dtype)
        
        elif tstep is None and zstep is not None:
            raise Exception('not implemented in -1,20')
            
        else:
            shape = (1, 1, var.ycount, var.xcount)
            zstri = var.ycount * var.xcount * 4
            pos   = var.strPos + zstri * zstep
            return __read_continuous(file, pos, shape, dtype)
    
    elif var.storage == '99' or var.storage == '0':
        if tstep is None and zstep is None:
            shape = (1, var.zcount, var.ycount, var.xcount)
            pos   = var.strPos
            data  = []
            
            for l in range(var.tcount):
                data.append(__read_continuous(file, pos, shape, dtype))
                pos += tstride
            
            return np.concatenate(data)
        
        elif zstep is None and tstep is not None:
            shape = (1, var.zcount, var.ycount, var.xcount)
            pos   = var.strPos + tstride * tstep
            data = __read_continuous(file, pos, shape, dtype)
            
            return data
        
        elif tstep is None and zstep is not None:
            raise Exception('not implemented in 0,99')
            
        else:
            shape = (1, 1, var.ycount, var.xcount)
            zstri = var.ycount * var.xcount * 4
            pos   = var.strPos + tstride * tstep + zstri * zstep
            return __read_continuous(file, pos, shape, dtype)
        
    else:
        raise Exception('invalid storage ' + var.storage +
                        ', only "99" or "-1,20" are supported')


def __read_continuous(file, offset=0, shape=None, dtype='<f4', use_mmap=True):
    """
    Read a block of continuous data into the memory.

    Parameters
    ----------
    file  : str
        A file from which data are read.
    offset: int
        An offset where the read is started.
    shape : tuple
        A tuple indicate shape of the Array returned.
    """
    with open(file, 'rb') as f:
        if use_mmap:
            data = np.memmap(f, dtype=dtype, mode='r', offset=offset,
                             shape=shape, order='C')
        else:
            number_of_values = reduce(lambda x, y: x*y, shape)
            f.seek(offset)
            data = np.fromfile(f, dtype=dtype, count=number_of_values)
            data = data.reshape(shape, order='C')
    
    data.shape = shape
    
    return data
