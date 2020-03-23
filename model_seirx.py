import pickle
import numpy as np
import torch
import pyro
from pyro.distributions import Uniform, Normal
from pyro.infer.mcmc import MCMC
from pyro.infer.mcmc.nuts import NUTS, HMC
from scipy.integrate import odeint
import matplotlib.pyplot as plt
from utils.eig import eig


class Model:
    def __init__(self, fdata="data/data.csv", day_offset=33):
        # vectors: exposed, infectious-dec, infectious-rec, dec, rec

        self.prior = {
            "t_incub": Uniform(0.1, 30.0),
            "inf_rate": Uniform(0.01, 1.0),
            "surv_rate": Uniform(0.01, 1.0),
            "t_dec": Uniform(0.1, 30.0),
            "t_rec": Uniform(0.1, 30.0),
        }
        self.vecnames = {
            "exposed": 0,
            "infectious_dec": 1,
            "infectious_rec": 2,
            "dec": 3,
            "rec": 4,
        }
        self.obsnames = ["gradient", "dec_by_rec", "dec_by_infection"]
        self.paramnames = list(self.prior.keys())

        self.nparams = len(self.paramnames)
        self.nobs = len(self.obsnames)

        # load the data
        self.obs = self.get_observable(fdata, day_offset)

    ###################### model specification ######################
    def construct_jac(self, params):
        t_incub, inf_rate, surv_rate, t_dec, t_rec = self.unpack(params)

        nparams = self.nparams
        K_rate = torch.zeros(nparams, nparams)
        K_rate[0,0] = -1./t_incub
        K_rate[0,1] = inf_rate
        K_rate[0,2] = inf_rate
        K_rate[1,0] = (1-surv_rate)/t_incub
        K_rate[1,1] = -1./t_dec
        K_rate[2,0] = surv_rate/t_incub
        K_rate[2,2] = -1./t_rec
        K_rate[3,1] = 1./t_dec
        K_rate[4,2] = 1./t_rec

        # K_rate = np.asarray([
        #     [-1./t_incub, inf_rate, inf_rate, 0.0, 0.0], # dn(exposed)/dt
        #     [(1-surv_rate)/t_incub, -1./t_dec, 0.0, 0.0, 0.0], # dn(infectious-dec)/dt
        #     [surv_rate/t_incub, 0.0, -1./t_rec, 0.0, 0.0], # dn(infectious-rec)/dt
        #     [0.0, 1./t_dec, 0.0, 0.0, 0.0], # dn(dec)/dt
        #     [0.0, 0.0, 1./t_rec, 0.0, 0.0], # dn(rec)/dt
        # ]) # (nfeat,nfeat)

        return K_rate

    ###################### observation specification ######################
    def get_simobservable(self, params):
        jac = self.construct_jac(params) # (nparams, nparams)
        eigvals, eigvecs = eig.apply(jac)
        max_eigvecs = eigvecs[:,-1] * torch.sign(eigvecs[-1,-1])

        # calculate the observable
        gradient = eigvals[-1] # the largest eigenvalue
        dec_by_rec = max_eigvecs[self.vecnames["dec"]] / max_eigvecs[self.vecnames["rec"]]
        dec_by_infection = max_eigvecs[self.vecnames["dec"]] / \
            (max_eigvecs[self.vecnames["infectious_rec"]] + max_eigvecs[self.vecnames["infectious_dec"]])
        return (gradient, dec_by_rec, dec_by_infection)

    def get_observable(self, fdata, day_offset):
        data0 = np.loadtxt(fdata, skiprows=1, delimiter=",", usecols=list(range(1,8))).astype(np.float32)
        data = data0[day_offset:,:]
        ninfectious = data[:,-3]
        nrec = data[:,-2]
        ndec = data[:,-1]
        ndays = data.shape[0]
        x = np.arange(ndays)

        # fit the infectious in the logplot
        logy = np.log(ninfectious)
        gradient, offset = np.polyfit(x, logy, 1)
        logyfit = offset + gradient * x
        std_gradient = np.sqrt(1./(x.shape[0]-2) * np.sum((logy - logyfit)**2) / np.sum((x-np.mean(x))**2))

        # the ratio of the graph
        dec_by_rec_mean = np.mean(ndec / nrec)
        dec_by_rec_std = np.std(ndec / nrec)
        dec_by_infection_mean = np.mean(ndec / ninfectious)
        dec_by_infection_std = np.std(ndec / ninfectious)

        # collect the distribution of the observation
        # obs_t_rec_total      = torch.tensor((18.0, 5.0))
        obs_gradient         = torch.tensor((gradient, std_gradient))
        obs_dec_by_rec       = torch.tensor((dec_by_rec_mean, dec_by_rec_std))
        obs_dec_by_infection = torch.tensor((dec_by_infection_mean, dec_by_infection_std))

        return (obs_gradient, obs_dec_by_rec, obs_dec_by_infection)

    ###################### util functions ######################
    def prior_params(self):
        return {name: pyro.sample(name, prior) for (name, prior) in self.prior.items()}

    def unpack(self, params):
        return [params[paramname] for paramname in self.paramnames]

    def inference(self): # a pytorch operation
        # get the parameters
        params = self.prior_params()
        simobs = self.get_simobservable(params)
        obs = self.obs # (nobs, 2)

        for i in range(self.nobs):
            pyro.sample(self.obsnames[i], Normal(simobs[i], obs[i][1]), obs=obs[i][0])

    ###################### postprocess ######################
    def sample_observations(self, samples):
        nsamples = len(samples[self.paramnames[0]])
        simobs = []
        for i in range(nsamples):
            params = {name: samples[name][i] for name in self.paramnames}
            simobs.append(self.get_simobservable(params)) # (nsamples, nobs)
        simobs = list(zip(*simobs)) # (nobs, nsamples)
        return np.asarray(simobs)

    def filter_samples(self, samples, filters_dict, filters_keys):
        idx = samples[self.paramnames[0]] > -float("inf")
        for key in filters_keys:
            filter_fcn = filters_dict[key]
            idx = idx * filter_fcn(samples)
        new_samples = {}
        for name in self.paramnames:
            new_samples[name] = samples[name][idx]
        return new_samples

    def plot_obs_inferece(self, simobs):
        # simobs (nobs, nsamples)

        nobs = self.nobs
        obs = self.obs
        nrows = int(np.sqrt(nobs*1.0))
        ncols = int(np.ceil((nobs*1.0) / nrows))
        for i in range(nobs):
            plt.subplot(nrows, ncols, i+1)
            plt.hist(simobs[i])
            plt.axvline(float(obs[i][0]), color='C1', linestyle='-')
            plt.axvline(float(obs[i][0])-float(obs[i][1]), color='C1', linestyle='--')
            plt.axvline(float(obs[i][0])+float(obs[i][1]), color='C1', linestyle='--')
            plt.title(self.obsnames[i])
        plt.show()

    def plot_samples(self, samples):
        nkeys = self.nparams
        nrows = int(np.sqrt(nkeys*1.0))
        ncols = int(np.ceil((nkeys*1.0) / nrows))
        for i in range(nkeys):
            plt.subplot(nrows, ncols, i+1)
            plt.hist(samples[self.paramnames[i]])
            plt.title(self.paramnames[i])
        plt.show()

class Model2(Model):
    def __init__(self, fdata="data/data.csv", day_offset=33):
        self.prior = {
            "t_incub": Uniform(0.1, 30.0),
            "inf_rate_unconf": Uniform(0.01, 1.0),
            "inf_rate_conf": Uniform(0.01, 1.0),
            "surv_rate": Uniform(0.01, 1.0),
            "t_conf": Uniform(0.1, 30.0),
            "t_dec_conf": Uniform(0.1, 30.0),
            "t_rec_conf": Uniform(0.1, 30.0),
            "t_dec_unconf": Uniform(0.1, 30.0),
            "t_rec_unconf": Uniform(0.1, 30.0),
        }
        self.vecnames = {
            "exposed": 0,
            "infectious_dec_unconf": 1,
            "infectious_rec_unconf": 2,
            "infectious_dec_conf": 3,
            "infectious_rec_conf": 4,
            "dec_conf": 5,
            "rec_conf": 6,
        }
        self.obsnames = ["gradient", "dec_by_rec", "dec_by_infection"]
        self.paramnames = list(self.prior.keys())

        self.nparams = len(self.paramnames)
        self.nobs = len(self.obsnames)

        # load the data
        self.obs = self.get_observable(fdata, day_offset)

    ###################### model specification ######################
    def construct_jac(self, params):
        t_incub, \
        inf_rate_unconf, \
        inf_rate_conf, \
        surv_rate, \
        t_conf, \
        t_dec_conf, \
        t_rec_conf, \
        t_dec_unconf, \
        t_rec_unconf = self.unpack(params)

        nparams = self.nparams
        K_rate = torch.zeros(nparams, nparams)
        K_rate[0,0] = -1./t_incub
        K_rate[0,1] = inf_rate_unconf
        K_rate[0,2] = inf_rate_unconf
        K_rate[0,3] = inf_rate_conf
        K_rate[0,4] = inf_rate_conf
        K_rate[1,0] = (1-surv_rate)/t_incub
        K_rate[1,1] = -1./t_dec_unconf - 1./t_conf
        K_rate[2,0] = surv_rate/t_incub
        K_rate[2,2] = -1./t_rec_unconf - 1./t_conf
        K_rate[3,1] = 1./t_conf
        K_rate[3,3] = -1./t_dec_conf
        K_rate[4,2] = 1./t_conf
        K_rate[4,4] = -1./t_rec_conf
        K_rate[5,3] = 1./t_dec_conf
        K_rate[6,4] = 1./t_rec_conf

        return K_rate

    ###################### observation specification ######################
    def get_simobservable(self, params):
        jac = self.construct_jac(params) # (nparams, nparams)
        eigvals, eigvecs = eig.apply(jac)
        max_eigvecs = eigvecs[:,-1] * torch.sign(eigvecs[-1,-1])

        # calculate the observable
        gradient = eigvals[-1] # the largest eigenvalue
        dec_by_rec = max_eigvecs[self.vecnames["dec_conf"]] / max_eigvecs[self.vecnames["rec_conf"]]
        dec_by_infection = max_eigvecs[self.vecnames["dec_conf"]] / \
            (max_eigvecs[self.vecnames["infectious_dec_conf"]] + max_eigvecs[self.vecnames["infectious_rec_conf"]])
        return (gradient, dec_by_rec, dec_by_infection)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default="model1")
    parser.add_argument("--infer", action="store_const", default=False, const=True)
    parser.add_argument("--filters", type=str, nargs="*")
    args = parser.parse_args()

    # get the mode of operation
    if args.infer:
        mode = "infer"
    else:
        mode = "display"

    # choose model
    day_offset = 33
    if args.model == "model1":
        model = Model(day_offset=day_offset)
        samples_fname = "pyro_samples.pkl"
    elif args.model == "model2":
        model = Model2(day_offset=day_offset)
        samples_fname = "pyro_samples_model2.pkl"

    if mode == "infer":
        hmc_kernel = NUTS(model.inference, step_size=0.1)
        posterior = MCMC(hmc_kernel,
                         num_samples=1000,
                         warmup_steps=50)
        posterior.run()
        samples = posterior.get_samples()
        with open(samples_fname, "wb") as fb:
            pickle.dump(samples, fb)

    with open(samples_fname, "rb") as fb:
        samples = pickle.load(fb)
    print("Collected %d samples" % len(samples[list(samples.keys())[0]]))

    filters_dict = {
        "low_infection_rate": lambda s: s["inf_rate"] < 0.5,
    }
    filter_keys = args.filters
    if filter_keys is not None:
        # filter the samples
        samples = model.filter_samples(samples, filters_dict, filter_keys)
        print("Filtered into %d samples" % len(samples[list(samples.keys())[0]]))

    # simobs: (nobs, nsamples)
    simobs = model.sample_observations(samples)

    # plot the observation
    model.plot_obs_inferece(simobs)
    model.plot_samples(samples)