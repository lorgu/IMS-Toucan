import os
import time

import librosa.display as lbd
import matplotlib.pyplot as plt
import torch
import torch.multiprocessing
from torch.cuda.amp import GradScaler
from torch.cuda.amp import autocast
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from Preprocessing.ArticulatoryCombinedTextFrontend import ArticulatoryCombinedTextFrontend
from Utility.WarmupScheduler import WarmupScheduler
from Utility.utils import cumsum_durations
from Utility.utils import delete_old_checkpoints


def plot_progress_spec(net, device, save_dir, step, lang, reference_speaker_embedding_for_plot):
    tf = ArticulatoryCombinedTextFrontend(language=lang)
    sentence = "Hello"
    if lang == "en":
        sentence = "This is an unseen sentence."
    elif lang == "de":
        sentence = "Dies ist ein ungesehener Satz."
    phoneme_vector = tf.string_to_tensor(sentence).squeeze(0).to(device)
    spec, durations, *_ = net.inference(text=phoneme_vector, speaker_embeddings=reference_speaker_embedding_for_plot, return_duration_pitch_energy=True)
    spec = spec.transpose(0, 1).to("cpu").numpy()
    duration_splits, label_positions = cumsum_durations(durations.cpu().numpy())
    if not os.path.exists(os.path.join(save_dir, "spec")):
        os.makedirs(os.path.join(save_dir, "spec"))
    fig, ax = plt.subplots(nrows=1, ncols=1)
    lbd.specshow(spec,
                 ax=ax,
                 sr=16000,
                 cmap='GnBu',
                 y_axis='mel',
                 x_axis=None,
                 hop_length=256)
    ax.yaxis.set_visible(False)
    ax.set_xticks(duration_splits, minor=True)
    ax.xaxis.grid(True, which='minor')
    ax.set_xticks(label_positions, minor=False)
    ax.set_xticklabels(tf.get_phone_string(sentence))
    ax.set_title(sentence)
    plt.savefig(os.path.join(os.path.join(save_dir, "spec"), str(step) + ".png"))
    plt.clf()
    plt.close()


def collate_and_pad(batch):
    # every entry in batch: [text, text_length, spec, spec_length, durations, energy, pitch, (speaker_embedding)]
    texts = list()
    text_lengths = list()
    spectrograms = list()
    spectrogram_lengths = list()
    durations = list()
    pitch = list()
    energy = list()
    if len(batch[0]) == 8:
        speaker_embeddings = list()
    for datapoint in batch:
        texts.append(datapoint[0])
        text_lengths.append(datapoint[1])
        spectrograms.append(datapoint[2])
        spectrogram_lengths.append(datapoint[3])
        durations.append(datapoint[4])
        energy.append(datapoint[5])
        pitch.append(datapoint[6])
        if len(batch[0]) == 8:
            speaker_embeddings.append(datapoint[7])
    if len(batch[0]) == 8:
        return (pad_sequence(texts, batch_first=True),
                torch.stack(text_lengths).squeeze(1),
                pad_sequence(spectrograms, batch_first=True),
                torch.stack(spectrogram_lengths).squeeze(1),
                pad_sequence(durations, batch_first=True),
                pad_sequence(pitch, batch_first=True),
                pad_sequence(energy, batch_first=True),
                torch.stack(speaker_embeddings))
    return (pad_sequence(texts, batch_first=True),
            torch.stack(text_lengths).squeeze(1),
            pad_sequence(spectrograms, batch_first=True),
            torch.stack(spectrogram_lengths).squeeze(1),
            pad_sequence(durations, batch_first=True),
            pad_sequence(pitch, batch_first=True),
            pad_sequence(energy, batch_first=True))


def train_loop(net,
               train_dataset,
               device,
               save_directory,
               batch_size=32,
               steps=300000,
               epochs_per_save=5,
               use_speaker_embedding=False,
               lang="en",
               lr=0.001,
               warmup_steps=14000,
               path_to_checkpoint=None,
               fine_tune=False):
    """
    :param steps: How many steps to train
    :param lr: The initial learning rate for the optimiser
    :param warmup_steps: how many warmup steps for the warmup scheduler
    :param path_to_checkpoint: reloads a checkpoint to continue training from there
    :param fine_tune: whether to load everything from a checkpoint, or only the model parameters
    :param lang: language of the synthesis
    :param use_speaker_embedding: whether to expect speaker embeddings
    :param net: Model to train
    :param train_dataset: Pytorch Dataset Object for train data
    :param device: Device to put the loaded tensors on
    :param save_directory: Where to save the checkpoints
    :param batch_size: How many elements should be loaded at once
    :param epochs_per_save: how many epochs to train in between checkpoints
    """
    net = net.to(device)
    if use_speaker_embedding:
        reference_speaker_embedding_for_plot = torch.Tensor(train_dataset[0][7]).to(device)
    else:
        reference_speaker_embedding_for_plot = None
    torch.multiprocessing.set_sharing_strategy('file_system')
    train_loader = DataLoader(batch_size=batch_size,
                              dataset=train_dataset,
                              drop_last=True,
                              num_workers=8,
                              pin_memory=False,
                              shuffle=True,
                              prefetch_factor=8,
                              collate_fn=collate_and_pad,
                              persistent_workers=True)
    step_counter = 0
    net.train()
    if fine_tune:
        lr = lr * 0.01
    optimizer = torch.optim.Adam(net.parameters(), lr=lr)
    scheduler = WarmupScheduler(optimizer, warmup_steps=warmup_steps)
    scaler = GradScaler()
    epoch = 0
    if path_to_checkpoint is not None:
        check_dict = torch.load(path_to_checkpoint, map_location=device)
        net.load_state_dict(check_dict["model"])
        if not fine_tune:
            optimizer.load_state_dict(check_dict["optimizer"])
            scheduler.load_state_dict(check_dict["scheduler"])
            step_counter = check_dict["step_counter"]
            scaler.load_state_dict(check_dict["scaler"])
    start_time = time.time()
    while True:
        epoch += 1
        optimizer.zero_grad()
        train_losses_this_epoch = list()
        for batch in tqdm(train_loader):
            with autocast():
                if not use_speaker_embedding:
                    train_loss = net(text_tensors=batch[0].to(device),
                                     text_lengths=batch[1].to(device),
                                     gold_speech=batch[2].to(device),
                                     speech_lengths=batch[3].to(device),
                                     gold_durations=batch[4].to(device),
                                     gold_pitch=batch[5].to(device),
                                     gold_energy=batch[6].to(device),
                                     step=step_counter)
                else:
                    train_loss = net(text_tensors=batch[0].to(device),
                                     text_lengths=batch[1].to(device),
                                     gold_speech=batch[2].to(device),
                                     speech_lengths=batch[3].to(device),
                                     gold_durations=batch[4].to(device),
                                     gold_pitch=batch[5].to(device),
                                     gold_energy=batch[6].to(device),
                                     speaker_embeddings=batch[7].to(device),
                                     step=step_counter)
                train_losses_this_epoch.append(float(train_loss))
            optimizer.zero_grad()
            scaler.scale(train_loss).backward()
            step_counter += 1
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0, error_if_nonfinite=False)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
        net.eval()
        if epoch % epochs_per_save == 0:
            torch.save({
                "model"       : net.state_dict(),
                "optimizer"   : optimizer.state_dict(),
                "step_counter": step_counter,
                "scaler"      : scaler.state_dict(),
                "scheduler"   : scheduler.state_dict(),
                }, os.path.join(save_directory, "checkpoint_{}.pt".format(step_counter)))
            delete_old_checkpoints(save_directory, keep=5)
            with torch.no_grad():
                plot_progress_spec(net, device, save_dir=save_directory, step=step_counter, lang=lang,
                                   reference_speaker_embedding_for_plot=reference_speaker_embedding_for_plot)
            if step_counter > steps:
                # DONE
                return
        print("Epoch:        {}".format(epoch + 1))
        print("Train Loss:   {}".format(sum(train_losses_this_epoch) / len(train_losses_this_epoch)))
        print("Time elapsed: {} Minutes".format(round((time.time() - start_time) / 60), 2))
        print("Steps:        {}".format(step_counter))
        net.train()
