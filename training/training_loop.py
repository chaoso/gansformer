# Training loop: 
# 1. Sets up the environment and data
# 2. Builds the generator (g) and discriminator (d) networks
# 3. Manage the training process
# 4. Run periodic evaluations on specified metrics 
# 5. Produces sample images over the course of training

# It supports training over data in TF records as produced by dataset_tool.py.
# Labels can optionally be provided although not essential
# If provided, image will be generated conditioned on a chosen label
import numpy as np
import tensorflow as tf
import dnnlib
import dnnlib.tflib as tflib
from dnnlib.tflib.autosummary import autosummary

from training import dataset as data
from training import misc
from training import visualize
from metrics import metric_base
import glob

# Data processing
# ----------------------------------------------------------------------------

# Just-in-time input image processing before feeding them to the networks
def process_reals(x, drange_data, drange_net, mirror_augment):
    with tf.name_scope("DynamicRange"):
        x = tf.cast(x, tf.float32)
        x.set_shape([None, 3, None, None])
        x = misc.adjust_dynamic_range(x, drange_data, drange_net)
    if mirror_augment:
        with tf.name_scope("MirrorAugment"):
            x = tf.where(tf.random_uniform([tf.shape(x)[0]]) < 0.5, x, tf.reverse(x, [3]))
    return x
    
def read_data(data, name, shape):
    var = tf.Variable(name = name, trainable = False, 
        initial_value = tf.zeros([sched.minibatch_gpu] + shape))
    data_write = tf.concat([data, var[minibatch_gpu_in:]], axis = 0)
    data_fetch_op = tf.assign(reals_var, data_write)
    data_read = var[:minibatch_gpu_in]
    return data_read, data_fetch_op

# Scheduling and optimization
# ----------------------------------------------------------------------------

# Evaluate time-varying training parameters
def training_schedule(
    sched_args,
    cur_nimg,                      # The training length, measured in number of generated images
    dataset,                       # The dataset object for accessing the data
    lrate_rampup_kimg  = 0,        # Duration of learning rate ramp-up
    tick_kimg          = 8):       # Default interval of progress snapshots

    # Initialize scheduling dictionary
    s = dnnlib.EasyDict()

    # Set parameters
    s.kimg = cur_nimg / 1000.0
    s.tick_kimg = tick_kimg
    s.resolution = 2 ** dataset.resolution_log2

    for arg in ["G_lrate", "D_lrate", "minibatch_size", "minibatch_gpu"]:
        s[arg] = sched_args[arg]

    # Learning rate optional rampup
    if lrate_rampup_kimg > 0:
        rampup = min(s.kimg / lrate_rampup_kimg, 1.0)
        s.G_lrate *= rampup
        s.D_lrate *= rampup

    return s

# Build two optimizers a network cN for the loss and regularization terms
def set_optimizer(cN, lrate_in, minibatch_multiplier, lazy_regularization = True, clip = None): 
    args = dict(cN.opt_args)
    args["minibatch_multiplier"] = minibatch_multiplier
    args["learning_rate"] = lrate_in
    if lazy_regularization:
        mb_ratio = cN.reg_interval / (cN.reg_interval + 1)
        args["learning_rate"] *= mb_ratio
        if "beta1" in args: args["beta1"] **= mb_ratio
        if "beta2" in args: args["beta2"] **= mb_ratio
    cN.opt = tflib.Optimizer(name = "Loss{}".format(cN.name), clip = clip, **args)
    cN.reg_opt = tflib.Optimizer(name = "Reg{}".format(cN.name), share = cN.opt, clip = clip, **args)

# Create optimization operations for computing and optimizing loss, gradient norm and regularization terms
def set_optimizer_ops(cN, lazy_regularization, no_op):
    cN.reg_norm = tf.constant(0.0)
    cN.trainables = cN.gpu.trainables

    if cN.reg is not None: 
        if lazy_regularization:
            cN.reg_opt.register_gradients(tf.reduce_mean(cN.reg * cN.reg_interval), cN.trainables)
            cN.reg_norm = cN.reg_opt.norm
        else:
            cN.loss += cN.reg

    cN.opt.register_gradients(tf.reduce_mean(cN.loss), cN.trainables)
    cN.norm = cN.opt.norm

    cN.loss_op = tf.reduce_mean(cN.loss) if cN.loss is not None else no_op
    cN.regval_op = tf.reduce_mean(cN.reg) if cN.reg is not None else no_op 
    cN.ops = {"loss": cN.loss_op, "reg": regval_op, "norm": cG.norm}

# Loading and logging
# ----------------------------------------------------------------------------

# Tracks exponential moving average: average, value -> new average
def emaAvg(avg, value, alpha = 0.995):
    if value is None:
        return avg
    if avg is None: 
        return value
    return avg * alpha + value * (1 - alpha)

# Load networks from snapshot
def load_nets(resume_pkl, lG, lD, lGs, recompile):
    print(misc.bcolored("Loading networks from %s..." % resume_pkl, "white"))
    rG, rD, rGs = misc.load_pkl(resume_pkl)[:3]
    if recompile: 
        print(misc.bold("Copying nets...")); 
        lG.copy_vars_from(rG); lD.copy_vars_from(rD); lGs.copy_vars_from(rGs)
    else: 
        lG, lD, lGs = rG, rD, rGs
    return lG, lD, lGs

# Training Loop 
# ----------------------------------------------------------------------------
# 1. Sets up the environment and data
# 2. Builds the generator (g) and discriminator (d) networks
# 3. Manage the training process
# 4. Run periodic evaluations on specified metrics 
# 5. Produces sample images over the course of training

def training_loop(
    # Configurations
    cG = {}, cD = {},                   # Generator and Discriminator command-line arguments
    dataset_args            = {},       # dataset.load_dataset() options
    sched_args              = {},       # train.TrainingSchedule options
    vis_args                = {},       # vis.eval options
    grid_args               = {},       # train.setup_snapshot_img_grid() options
    metric_arg_list         = [],       # MetricGroup Options
    tf_config               = {},       # tflib.init_tf() options
    eval                    = False,    # Evaluation mode
    train                   = True,     # Training mode
    # Data
    data_dir                = None,     # Directory to load datasets from
    total_kimg              = 25000,    # Total length of the training, measured in thousands of real images
    mirror_augment          = False,    # Enable mirror augmentation?
    drange_net              = [-1,1],   # Dynamic range used when feeding image data to the networks
    # Optimization
    minibatch_repeats       = 4,        # Number of minibatches to run before adjusting training parameters
    lazy_regularization     = True,     # Perform regularization as a separate training step?
    smoothing_kimg          = 10.0,     # Half-life of the running average of generator weights
    clip                    = None,     # Clip gradients threshold
    # Resumption
    resume_pkl              = None,     # Network pickle to resume training from, None = train from scratch.
    resume_kimg             = 0.0,      # Assumed training progress at the beginning. Affects reporting and training schedule.
    resume_time             = 0.0,      # Assumed wallclock time at the beginning. Affects reporting.
    recompile               = False,    # Recompile network from source code (otherwise loads from snapshot)
    # Logging
    summarize               = True,     # Create TensorBoard summaries   
    keep_samples            = False,    # Keep all prior samples during training
    save_tf_graph           = False,    # Include full TensorFlow computation graph in the tfevents file?
    save_weight_histograms  = False,    # Include weight histograms in the tfevents file?
    img_snapshot_ticks      = 3,        # How often to save image snapshots? None = only save "reals.png" and "fakes-init.png".
    network_snapshot_ticks  = 3,        # How often to save network snapshots? None = only save "networks-final.pkl".
    last_snapshots          = 10,
    eval_size               = 100,      # Number of samples for evaluation
    rich_eval_size          = 10,       # Number of samples for rich evaluation (heavier images)
    printname               = "",       # Experiment name for logging
    # Architecture
    component_num           = 1,        # Number of generator latent components (1 for StyleGAN)
    attention               = False,    # Introduce transformer layers
    merge                   = False):   # Generate several images and then merge them

    # Initialize dnnlib and TensorFlow
    tflib.init_tf(tf_config)
    num_gpus = dnnlib.submit_config.num_gpus
    cG.name, cD.name = "g", "d"

    # Load dataset
    dataset = data.load_dataset(data_dir = dnnlib.convert_path(data_dir), verbose = True, **dataset_args)
    # Labels are optional but not essential
    grid_size, grid_reals, grid_labels = misc.setup_snapshot_img_grid(dataset, **grid_args)
    grid_latents = np.random.randn(np.prod(grid_size), *G.input_shape[1:])
    misc.save_img_grid(grid_reals, dnnlib.make_run_dir_path("reals.png"), drange = dataset.dynamic_range, grid_size = grid_size)
    sched = training_schedule(sched_args, cur_nimg = total_kimg * 1000, dataset = dataset)

    # Construct or load networks
    with tf.device("/gpu:0"):
        no_op = tf.no_op()
        G, D = None, None
        if resume_pkl is None or recompile:
            print(misc.bcolored("Constructing networks...", "white"))
            G = tflib.Network("G", num_channels = dataset.shape[0], resolution = dataset.shape[1], label_size = dataset.label_size, **cG.args)
            D = tflib.Network("D", num_channels = dataset.shape[0], resolution = dataset.shape[1], label_size = dataset.label_size, **cD.args)
            Gs = G.clone("Gs")              

            if resume_pkl is not None:
                G, D, Gs = load_nets(resume_pkl, G, D, Gs, recompile)

    G.print_layers(); D.print_layers()
    
    # Train/Evaluate
    if eval: visualize.eval(G, dataset, batch_size = sched.minibatch_gpu, 
        drange_net = drange_net, **vis_args)
    if not train:
        dataset.close()        
        exit()

    # Setup training inputs
    print(misc.bcolored("Building TensorFlow graph...", "white"))
    with tf.name_scope("Inputs"), tf.device("/cpu:0"):
        lrate_in_g             = tf.placeholder(tf.float32, name = "lrate_in_g", shape = [])
        lrate_in_d             = tf.placeholder(tf.float32, name = "lrate_in_d", shape = [])
        step                 = tf.placeholder(tf.int32, name = "step", shape = [])
        minibatch_size_in    = tf.placeholder(tf.int32, name = "minibatch_size_in", shape=[])
        minibatch_gpu_in     = tf.placeholder(tf.int32, name = "minibatch_gpu_in", shape=[])
        minibatch_multiplier = minibatch_size_in // (minibatch_gpu_in * num_gpus)
        beta                 = 0.5 ** tf.div(tf.cast(minibatch_size_in, tf.float32), smoothing_kimg * 1000.0) if smoothing_kimg > 0.0 else 0.0

    # Set optimizers
    for cN, lr in [(cG, lrate_in_g), (cD, lrate_in_d)]:
        set_optimizer(cN, lr, minibatch_multiplier, lazy_regularization, clip) 

    # Build training graph for each GPU
    data_fetch_ops = []
    for gpu in range(num_gpus):
        with tf.name_scope("GPU%d" % gpu), tf.device("/gpu:%d" % gpu):

            # Create GPU-specific shadow copies of G and D
            for cN, N in [(cG, G), (cD, D)]:
                cN.gpu = N if gpu == 0 else N.clone(N.name + "_shadow")
            Gs_gpu = Gs if gpu == 0 else Gs.clone(Gs.name + "_shadow")

            # Fetch training data via temporary variables
            with tf.name_scope("DataFetch"):
                sched = training_schedule(cur_nimg = int(resume_kimg * 1000), dataset = dataset, **sched_args)
                reals, labels_write = dataset.get_minibatch_tf()        
                reals = process_reals(reals, dataset.dynamic_range, drange_net, mirror_augment)
                reals, reals_fetch = read_data(reals, "reals", dataset.shape)
                labels, labels_fetch = read_data(labels, "labels", [dataset.label_size])
                data_fetch_ops += [reals_fetch, labels_fetch]

            # Evaluate loss functions
            with tf.name_scope("G_loss"):
                cG.loss, cG.reg = dnnlib.util.call_func_by_name(G = cG.gpu, D = cD.gpu, 
                    dataset = dataset, reals = reals_read, 
                    minibatch_size = minibatch_gpu_in, **cG.loss_args)

            with tf.name_scope("D_loss"):
                cD.loss, cD.reg = dnnlib.util.call_func_by_name(G = cG.gpu, D = cD.gpu, 
                    dataset = dataset, reals = reals_read, labels = labels_read, 
                    minibatch_size = minibatch_gpu_in, **cD.loss_args)

            for cN in [cG, cD]:
                set_optimizer_ops(cN, lazy_regularization, no_op)

    # Setup training ops
    data_fetch_op = tf.group(*data_fetch_ops)
    for cN in [cG, cD]:
        cN.train_op = cN.opt.apply_updates()
        cN.reg_op = cN.reg_opt.apply_updates(allow_no_op = True)
    Gs_update_op = Gs.setup_as_moving_average_of(G, beta = beta)

    # Finalize graph
    with tf.device("/gpu:0"):
        try:
            peak_gpu_mem_op = tf.contrib.memory_stats.MaxBytesInUse()
        except tf.errors.NotFoundError:
            peak_gpu_mem_op = tf.constant(0)
    tflib.init_uninitialized_vars()

    # Tensorboard summaries 
    if summarize:
        print(misc.bcolored("Initializing logs...", "white"))
        summary_log = tf.summary.FileWriter(dnnlib.make_run_dir_path())
        if save_tf_graph:
            summary_log.add_graph(tf.get_default_graph())
        if save_weight_histograms:
            G.setup_weight_histograms(); D.setup_weight_histograms()

    metrics = metric_base.MetricGroup(metric_arg_list, dataset = dataset)

    # Initialize training
    print(misc.bcolored("Training for %d kimg..." % total_kimg, "white"))
    dnnlib.RunContext.get().update("", cur_epoch = resume_kimg, max_epoch = total_kimg)
    maintenance_time = dnnlib.RunContext.get().get_last_update_interval()

    cur_tick, running_mb_counter = -1, 0
    cur_nimg = int(resume_kimg * 1000)
    tick_start_nimg = cur_nimg
    for cN in [cG, cD]:
        cN.lossvals_agg = {k: None for k in ["loss", "reg", "norm", "reg_norm"]} 

    # Training loop
    while cur_nimg < total_kimg * 1000:
        if dnnlib.RunContext.get().should_stop(): 
            break

        # Choose training parameters and configure training ops
        sched = training_schedule(cur_nimg = cur_nimg, dataset = dataset, **sched_args)
        assert sched.minibatch_size % (sched.minibatch_gpu * num_gpus) == 0
        dataset.configure(sched.minibatch_gpu)
        for cN in [cG, cD]:
            cN.opt.reset_optimizer_state()

        # Run training ops
        feed_dict = {
            lrate_in_g: sched.G_lrate, 
            lrate_in_d: sched.D_lrate, 
            minibatch_size_in: sched.minibatch_size, 
            minibatch_gpu_in: sched.minibatch_gpu,
            step: sched.kimg
        }

        # Several iterations before updating training parameters
        for _repeat in range(minibatch_repeats):
            rounds = range(0, sched.minibatch_size, sched.minibatch_gpu * num_gpus)
            for cN in [cG, cD]:
                cN.run_reg = lazy_regularization and (running_mb_counter % cN.reg_interval == 0)
            cur_nimg += sched.minibatch_size
            running_mb_counter += 1

            for cN in [cG, cD]:
                cN.lossvals = {k: None for k in ["loss", "reg", "norm", "reg_norm"]} 

            # Gradient accumulation
            for _round in rounds:
                cG.lossvals.update(tflib.run([cG.train_op, cG.ops], feed_dict)[1])
                if cG.run_reg:
                    _, cG.lossvals[3] = tflib.run([cG.reg_op, cG.reg_norm], feed_dict) 

                tflib.run(data_fetch_op, feed_dict)

                cD.lossvals.update(tflib.run([cD.train_op, cD.ops], feed_dict)[1])
                if cD.run_reg:
                    _, cD.lossvals[3] = tflib.run([cD.reg_op, cD.reg_norm], feed_dict) 

            tflib.run([Gs_update_op], feed_dict)

            # Track loss statistics 
            for cN in [cG, cD]:
                for i in range(len(cN.lossvals_agg)):
                    cN.lossvals_agg[i] = emaAvg(cN.lossvals_agg[i], cN.lossvals[i])

        # Perform maintenance tasks once per tick
        done = (cur_nimg >= total_kimg * 1000)
        if cur_tick < 0 or cur_nimg >= tick_start_nimg + sched.tick_kimg * 1000 or done:
            cur_tick += 1
            tick_kimg = (cur_nimg - tick_start_nimg) / 1000.0
            tick_start_nimg = cur_nimg
            tick_time = dnnlib.RunContext.get().get_time_since_last_update()
            total_time = dnnlib.RunContext.get().get_time_since_start() + resume_time

            # Report progress
            print("tick %s kimg %s minibatch %s loss/reg: G (%s %s) D (%s %s), norms: G (%s %s) D (%s %s) time %s sec/tick %s sec/kimg %s %s" % (
                misc.bold("%-5d" % autosummary("Progress/tick", cur_tick)),
                misc.bcolored("%-8.1f" % autosummary("Progress/kimg", cur_nimg / 1000.0), "red"),
                "%-4d" % autosummary("Progress/minibatch", sched.minibatch_size),
                misc.bcolored("%.3f" % (cG.lossvals_agg["loss"] or -1), "blue"),
                misc.bold("%.3f" % (cG.lossvals_agg["reg"] or -1)),
                misc.bcolored("%.3f" % (cD.lossvals_agg["loss"] or -1), "blue"),
                misc.bold("%.3f" % (cD.lossvals_agg["reg"] or -1)),
                misc.cond_bcolored(cG.lossvals_agg["norm"], 20.0, "red"),
                misc.cond_bcolored(cG.lossvals_agg["reg_norm"], 20.0, "red"),
                misc.cond_bcolored(cD.lossvals_agg["norm"], 20.0, "red"),
                misc.cond_bcolored(cD.lossvals_agg["reg_norm"], 20.0, "red"),
                misc.bold("%-12s" % dnnlib.util.format_time(autosummary("Timing/total_sec", total_time))),
                "%-7.1f" % autosummary("Timing/sec_per_tick", tick_time),
                "%-7.2f" % autosummary("Timing/sec_per_kimg", tick_time / tick_kimg),
                printname))

            autosummary("Timing/total_hours", total_time / (60.0 * 60.0))
            autosummary("Timing/total_days", total_time / (24.0 * 60.0 * 60.0))

            # Save snapshots
            if img_snapshot_ticks is not None and (cur_tick % img_snapshot_ticks == 0 or done):
                visualize.eval(G, dataset, batch_size = sched.minibatch_gpu, training = True, 
                    step = cur_nimg // 1000, eval_size = grid_size, latents = grid_latents, 
                    labels = grid_labels, drange_net = drange_net, **vis_args)

            if network_snapshot_ticks is not None and (cur_tick % network_snapshot_ticks == 0 or done):
                pkl = dnnlib.make_run_dir_path("network-snapshot-%06d.pkl" % (cur_nimg // 1000))
                misc.save_pkl((G, D, Gs), pkl, remove = False)
                
                if cur_tick % network_snapshot_ticks == 0 or done:
                    metric = metrics.run(pkl, run_dir = dnnlib.make_run_dir_path(), data_dir = dnnlib.convert_path(data_dir), 
                        num_gpus = num_gpus, tf_config = tf_config)

                if last_snapshots > 0:
                    misc.rm(sorted(glob.glob(dnnlib.make_run_dir_path("network*.pkl")))[:-last_snapshots])

            # Update summaries and RunContext
            if summarize:
                metrics.update_autosummaries()
                tflib.autosummary.save_summaries(summary_log, cur_nimg)
            
            dnnlib.RunContext.get().update(None, cur_epoch = cur_nimg // 1000, max_epoch = total_kimg)
            maintenance_time = dnnlib.RunContext.get().get_last_update_interval() - tick_time

    # Save final snapshot
    misc.save_pkl((G, D, Gs), dnnlib.make_run_dir_path("network-final.pkl"), remove = False)

    # All done
    if summarize:
        summary_log.close()
    dataset.close()
