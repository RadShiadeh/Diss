import time
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from models import ResNetClassifier, ETEResNet, ResNetFinalClassifier, ResNetFinalScorer
from dataloader_aug import VideoDataset
import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score


def evaluate_scorer(classifier_, scorer_, ete, data_loader):
    classifier_.eval()
    scorer_.eval()
    ete.eval()
    predicted_scores = []
    true_scores = []
    with torch.no_grad():
        for batch_data in data_loader:
            frames = batch_data[0].type(torch.FloatTensor).to(device)
            frames = frames.permute(0, 4, 1, 2, 3)
            score_labels = batch_data[2].type(torch.FloatTensor).to(device)


            output = eteModel(frames)
            out_score = output['final_score']

            predicted_scores.extend(out_score.cpu().numpy())
            true_scores.extend(score_labels.cpu().numpy())

    predicted_scores = np.array(predicted_scores)
    true_scores = np.array(true_scores)

    classifier_.train()
    scorer_.train()
    ete.train()

    return predicted_scores, true_scores

def get_accuracy_classification(ete, test_data):
    correct = 0
    total = 0
    ete.eval()

    true_labels = []
    predicted_probs = []

    with torch.no_grad():
        for data in test_data:
            frames = data[0].type(torch.FloatTensor).to(device)
            frames = frames.permute(0, 4, 1, 2, 3)
            classification_labels = data[1].type(torch.FloatTensor).to(device)

            outputs = ete(frames)

            _, pred = torch.max(outputs['classification'], 1)
            total += classification_labels.size(0)
            correct += (pred == classification_labels).sum().item()

            pred_probs = torch.sigmoid(outputs['classification'])

            true_labels.extend(classification_labels.cpu().numpy())
            predicted_probs.extend(pred_probs.cpu().numpy())
        
    true_labels = np.array(true_labels)
    predicted_probs = np.array(predicted_probs)

    auc_score = roc_auc_score(true_labels, predicted_probs)

    accuracy = correct / total * 100
    ete.train()
    
    return accuracy, auc_score




def print_metrics(epoch, loss, accuracy, type, epoch_end, auc=0):
    print(
        f"epoch: [{epoch}], "
        f"batch loss: {loss:.3f}, "
        f"accuracy: {accuracy:.3f}, "
        f"model type: {type}"
        f"epoch end time: {epoch_end:.3f}, "
        f"auc : {auc:.3f}"
    )


def log_metrics(epoch, loss, data_load_time, step_time):
    summary_writer.add_scalar("epoch", epoch, step)
    summary_writer.add_scalars(
                "loss",
            {"train": float(loss.item())},
            step
    )
    summary_writer.add_scalar(
            "time/data", data_load_time, step
    )
    summary_writer.add_scalar(
            "time/data", step_time, step
    )


if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

step = 0
log_frequency = 5
running_loss_print_freq = 50
print_frequency = 1
batch_size = 16
eval_freq = 1
c3d_pkl_path = "../../dissData/c3d.pickle"

train_labels_path = "../labels/train_labels/train.pkl"
train_vids = "../../dissData/video_npy/train"
video_dataset = VideoDataset(train_vids, train_labels_path, transform=None, num_frames=16)

labels_valid = "../labels/valid_labels/valid.pkl"
valid_vids = "../../dissData/video_npy/valid"
video_dataset_valid = VideoDataset(valid_vids, labels_valid, transform=None, num_frames=16)

labels_test = "../labels/test_labels/test.pkl"
test_vids = "../../dissData/video_npy/test"
video_dataset_test = VideoDataset(test_vids, labels_test, transform=None, num_frames=16)

train_data_loader = DataLoader(video_dataset, batch_size=batch_size, shuffle=True)
validation_data = DataLoader(video_dataset_valid, batch_size)
test_data_loader = DataLoader(video_dataset_test, batch_size)

resNet = ResNetClassifier()
final_class = ResNetFinalClassifier()
score_reg = ResNetFinalScorer()

score_reg = score_reg.to(device)
resNet = resNet.to(device)
final_class = final_class.to(device)

eteModel = ETEResNet(resNet, final_class, score_reg)

eteModel = eteModel.to(device)


criterion_classification = nn.BCELoss()
criterion_scorer = nn.MSELoss()
criterion_scorer_penalty = nn.L1Loss()

optim_params = (list(resNet.parameters()) + list(score_reg.parameters()) + list(final_class.parameters()))
optimizer = optim.AdamW(optim_params, lr=0.0001)


summary_writer = SummaryWriter()

num_epochs = 50
for epoch in range(num_epochs):
    epoch_start_time = time.time()
    print('-------------------------------------------------------------------------------------------------------')
    data_load_start_time = time.time()
    classification_running_loss = 0.0
    scorer_running_loss = 0.0
    score_reg.train()
    final_class.train()
    eteModel.train()

    for _, batch_data in enumerate(train_data_loader):
        frames = batch_data[0].type(torch.FloatTensor).to(device)
        frames = frames.permute(0, 4, 1, 2, 3)
        classification_labels = batch_data[1].type(torch.FloatTensor).to(device)
        score_labels = batch_data[2].type(torch.FloatTensor).to(device)
        score_labels = score_labels.float().view(-1, 1)

        data_load_end_time = time.time()

        optimizer.zero_grad()

        output = eteModel(frames)

        classification_output = output['classification']
        final_score_output = output['final_score']

        classification_loss = criterion_classification(classification_output, classification_labels.float().view(-1, 1))
        final_score_loss = criterion_scorer(final_score_output, score_labels.float()) + criterion_scorer_penalty(final_score_output, score_labels.float())


        loss = 0
        loss += final_score_loss
        loss += classification_loss

        loss.backward()
        optimizer.step()

        classification_running_loss += classification_loss.item()
        scorer_running_loss += final_score_loss.item()

        data_load_time = data_load_end_time - data_load_start_time
        step_time = time.time() - data_load_end_time

        if ((step + 1) % log_frequency) == 0:
            log_metrics(epoch, classification_loss, data_load_time, step_time)
            log_metrics(epoch, final_score_loss, data_load_time, step_time)

        step += 1
    
    epoch_end_time = time.time()
    epoch_time = epoch_end_time - epoch_start_time

    if ((epoch + 1) % eval_freq) == 0:
        pred_score, true_score = evaluate_scorer(final_class, score_reg, eteModel, validation_data)
        correlation_coeff, _ = spearmanr(pred_score, true_score)
        accuracy_class, auc_class = get_accuracy_classification(eteModel, validation_data)
    
    avg_classification_loss = classification_running_loss / len(train_data_loader)
    avg_scorer_loss = scorer_running_loss / len(train_data_loader)

    if ((epoch + 1) % print_frequency) == 0:
        print_metrics(epoch=epoch+1, loss=avg_classification_loss, accuracy=accuracy_class, type="classification ", epoch_end=epoch_time, auc=auc_class)
        print_metrics(epoch=epoch+1, loss=avg_scorer_loss, accuracy=correlation_coeff, type="scorer spearmanr correlation ", epoch_end=epoch_time)
        print(f"running losses: {classification_running_loss, scorer_running_loss} [class, scorer]")

    if (epoch + 1) % 5 == 0:
        torch.save(eteModel.state_dict(), 'ETE_model_resNet.pth')
    
    

summary_writer.close()
