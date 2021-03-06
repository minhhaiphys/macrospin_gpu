#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Numerical libraries
import numpy as np

# Tempalting imports
import jinja2 as jj
from   jinja2 import Template

# Physics imports
from   macrospin_gpu.demag import demagCylinder

# Built ins
import os

# Physical Constants in CGS
# Watch out for AbAmps!
ech   = 1.6022e-20
hbar  = 6.6261e-27 / (2.0*np.pi)
muB   = 9.2740e-21
kB    = 1.3807e-16
g     = 2.0
gamma = g*muB / hbar

class Macrospin_2DPhaseDiagram(object):
    """Wrapper object for macrospin OpenCL kernel"""
    def __init__(self):
        super(Macrospin_2DPhaseDiagram, self).__init__()

        # Dict that passes all parameters to the kernel template
        self.parameters = {}

        # List of spin torques
        self.spin_torques = []

        # Current state
        self.current_iter      = 0
        self.current_time      = 0.0
        self.current_timepoint = 0

        # Thermal evolution?
        self.temperature          = 0.0
        self.thermal_realizations = 1

        # Store times?
        self.time_traces = False

        # Default field
        self.parameters['hext'] = [0,0,0,0]

        # Main template
        self.dirname = os.path.dirname(__file__)
        with open(self.dirname+"/templates/kernel_template_heun_spherical_2D.cl") as template_file:
            self.main_template = template_file.read()

    def set_evolution_properties(self, dt=1e-13, initial_pause=0.2e-9, total_time=1.4e-9):
        if not hasattr(self, 'Ms'):
            raise Exception("Must set magnetic properties before evolution properties")
        timeUnit     = (1.0+self.damping**2)/(gamma*self.Ms)    # Reduced units for numerical convnience
        self.real_dt = dt                     # Actual time step (s)
        self.dt      = dt/timeUnit            # time in units of $\gamma M_s$
        self.total_time         = total_time
        self.total_steps        = int(total_time/self.real_dt)
        # self.normalize_interval = normalize_interval

        self.parameters['initial_pause'] = initial_pause
        self.parameters['dt'] = self.dt
        self.parameters['real_dt'] = self.real_dt

    def store_time_traces(self, interval=10.0e-12):
        if not hasattr(self, 'real_dt'):
            raise Exception("Must set evolution properties before setting up time traces.")
        self.time_traces = True
        self.m_of_t_update_interval = int(interval/self.real_dt)
        self.time_points = int(self.total_time/interval)

    def set_magnetic_properties(self, Ms=640.0, Hpma=0.0, Hk=0.0, Hd=0.0, damping=0.05, initial_theta=np.pi/2, initial_phi=0.0):
        self.Ms      = Ms
        self.Hpma    = Hpma
        self.Hk      = Hk
        self.Hd      = Hd
        self.damping = damping
        self.initial_theta = initial_theta
        self.initial_phi = initial_phi
        self.parameters['Ms']    = Ms
        self.parameters['hk']    = Hk/Ms
        self.parameters['hd']    = (Hd - Hpma)/Ms
        self.parameters['alpha'] = damping

    def set_external_field(self, h):
        assert len(h) == 3
        h.append(0)
        self.parameters['hext'] = [hi/self.Ms for hi in h]

    def set_geometry(self, length, width, thickness):
        # Specified in nm, converted to cm
        self.length    = length*1e-7
        self.width     = width*1e-7
        self.thickness = thickness*1e-7
        self.area      = np.pi*self.length*self.width/4.0
        self.vol       = self.area*self.thickness

        if not hasattr(self, 'Hpma'):
            raise Exception("Must set magnetic properties before defining geometry.")

        # Nxx, Nyy, Nzz  = demagCylinder(length, width, thickness, cgs=True)
        # Nzz            = Nzz - self.Hpma/self.Ms
        # # self.parameters['demag_tensor'] = [Nxx, Nyy, Nzz, 0.0]

    def add_spin_torque(self, pol_vector, pol_strength, lambda_asymm,
                        current_density=0.5e8, pulse_duration=1e-9,
                        square_pulse=True, rise_time=60.0e-12, fall_time=110.0e-12):

        self.parameters['stt'] = True
        this_torque = {}

        prefactor = 0.1*2.0*(lambda_asymm**2)*pol_strength*hbar # 0.1 for Amps->abAmps
        prefactor = prefactor*(1.0-self.damping)/(2.0*ech*self.Ms*self.Ms*self.thickness)

        assert len(pol_vector) == 3

        this_torque['pol_x']  = pol_vector[0]
        this_torque['pol_y']  = pol_vector[1]
        this_torque['pol_z']  = pol_vector[2]
        this_torque['l2_p1']  = lambda_asymm**2 + 1.0
        this_torque['l2_m1']  = lambda_asymm**2 - 1.0
        this_torque['prefac'] = prefactor
        self.spin_torques.append(this_torque)

        self.parameters['square_pulse'] = square_pulse
        self.parameters['rise_time']    = rise_time
        self.parameters['fall_time']    = fall_time

        self.parameters['stt_torques']     = self.spin_torques
        self.parameters['current_density'] = current_density
        self.parameters['pulse_duration']  = pulse_duration

    def enable_oersted_field(self, field_direction=[1,0,0]):
        if not hasattr(self, 'thickness'):
            raise Exception("Must set geometry before enabling oersted field.")
        self.parameters['oersted'] = True
        self.parameters['h_oe_prefac'] = self.thickness*0.5/self.Ms
        self.parameters['h_oe_x']  = float(field_direction[0])
        self.parameters['h_oe_y']  = float(field_direction[1])
        self.parameters['h_oe_z']  = float(field_direction[2])

    def add_thermal_noise(self, temperature, thermal_realizations=16):
        self.temperature          = temperature
        self.thermal_realizations = thermal_realizations

        # Width of thermal distribution
        self.nu = np.sqrt(2.0*self.damping*kB*self.temperature/(self.vol*self.Ms**2))
        self.parameters['thermal']  = self.temperature > 0
        self.parameters['nu_prime'] = self.nu/np.sqrt(self.dt)
        # self.parameters['nu']       = self.nu
        # self.parameters['nu2']      = self.nu**2

    def define_phase_diagram(self, first_parameter_name, first_parameter_values,
                                   second_parameter_name, second_parameter_values):

        assert first_parameter_name in self.parameters
        assert second_parameter_name in self.parameters
        self.parameters.pop(first_parameter_name)
        self.parameters.pop(second_parameter_name)
        self.parameters['first_loop_var'] = first_parameter_name
        self.parameters['second_loop_var'] = second_parameter_name

        self.first_val_steps = len(first_parameter_values)
        self.second_val_steps = len(second_parameter_values)

        self.first_vals_np  = first_parameter_values.astype(np.float32)
        self.second_vals_np = second_parameter_values.astype(np.float32)

    def render_kernel(self):
        # Define some final constants
        self.pixels = self.first_val_steps*self.second_val_steps
        self.N      = self.first_val_steps*self.second_val_steps*self.thermal_realizations

        # Convert parameters into global constants for the OpenCL code
        self.float_parameters  = [{'name': k, 'value': v} for k,v in self.parameters.items() if isinstance(v, float)]
        self.float3_parameters = [{'name': k, 'x': float(v[0]), 'y': float(v[1]), 'z': float(v[2])} for k,v in self.parameters.items() if isinstance(v, list) and len(v)==3]
        self.float4_parameters = [{'name': k, 'x': float(v[0]), 'y': float(v[1]), 'z': float(v[2]), 'w': float(v[3])} for k,v in self.parameters.items() if isinstance(v, list) and len(v)==4]

        self.parameters['float_constants']  = self.float_parameters
        self.parameters['float3_constants'] = self.float3_parameters
        self.parameters['float4_constants'] = self.float4_parameters

        template = jj.Template(self.main_template)
        rendered_kernel = template.render(**self.parameters)
        return rendered_kernel
