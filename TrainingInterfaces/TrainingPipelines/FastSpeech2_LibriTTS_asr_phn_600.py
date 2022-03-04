import random

import torch
from torch.utils.data import ConcatDataset
from tqdm import tqdm

from TrainingInterfaces.Text_to_Spectrogram.FastSpeech2.FastSpeech2 import FastSpeech2
from TrainingInterfaces.Text_to_Spectrogram.FastSpeech2.fastspeech2_train_loop import train_loop
from Utility.corpus_preparation import prepare_fastspeech_corpus
from Utility.path_to_transcript_dicts import *


def run(gpu_id, resume_checkpoint, finetune, model_dir, resume):
    if gpu_id == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        device = torch.device("cpu")

    else:
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = "{}".format(gpu_id)
        device = torch.device("cuda")

    torch.manual_seed(131714)
    random.seed(131714)
    torch.random.manual_seed(131714)

    print("Preparing")

    if model_dir is not None:
        save_dir = model_dir
    else:
        save_dir = os.path.join("Models", "FastSpeech2_LibriTTS_asr_phn_600")
    os.makedirs(save_dir, exist_ok=True)

    train_set = prepare_fastspeech_corpus(transcript_dict=build_path_to_transcript_dict_libritts_asr_phn_500(),
                                          corpus_dir=os.path.join("Corpora", "libri_asr_phn_500"),
                                          lang="en",
                                          phone_input=True,
                                          ctc_selection=False)

    model = FastSpeech2(lang_embs=None)

    find_faulty_samples(model, train_set, device, "Models/FastSpeech2_LibriTTS_asr_phn/best.pt")

    train_sets = list()
    train_sets.append(train_set)
    train_sets.append(prepare_fastspeech_corpus(transcript_dict=build_path_to_transcript_dict_libritts_asr_phn(),
                                                corpus_dir=os.path.join("Corpora", "libri_asr_phn"),
                                                lang="en",
                                                phone_input=True,
                                                ctc_selection=False))

    train_set = ConcatDataset(train_sets)

    print("Training model")
    train_loop(net=model,
               train_dataset=train_set,
               device=device,
               save_directory=save_dir,
               steps=500000,
               batch_size=32,
               lang="en",
               lr=0.0001,
               warmup_steps=4000,
               path_to_checkpoint="Models/FastSpeech2_LibriTTS_asr_phn/best.pt",
               fine_tune=True,
               resume=resume)


@torch.inference_mode()
def find_faulty_samples(net,
                        dataset,
                        device,
                        path_to_checkpoint):
    nan_ids = list()
    net = net.to(device)
    torch.multiprocessing.set_sharing_strategy('file_system')
    check_dict = torch.load(os.path.join(path_to_checkpoint), map_location=device)
    net.load_state_dict(check_dict["model"])
    for datapoint_index in tqdm(range(len(dataset))):
        loss = net(text_tensors=dataset[datapoint_index][0].unsqueeze(0).to(device),
                   text_lengths=dataset[datapoint_index][1].to(device),
                   gold_speech=dataset[datapoint_index][2].unsqueeze(0).to(device),
                   speech_lengths=dataset[datapoint_index][3].to(device),
                   gold_durations=dataset[datapoint_index][4].unsqueeze(0).to(device),
                   gold_pitch=dataset[datapoint_index][6].unsqueeze(0).to(device),  # mind the switched order
                   gold_energy=dataset[datapoint_index][5].unsqueeze(0).to(device),  # mind the switched order
                   utterance_embedding=dataset[datapoint_index][7].unsqueeze(0).to(device),
                   lang_ids=None,
                   return_mels=False).squeeze()
        if torch.isnan(loss):
            print(f"CAREFUL, NAN DETECTED: {datapoint_index}")
            nan_ids.append(datapoint_index)
    dataset.remove_samples(nan_ids)
