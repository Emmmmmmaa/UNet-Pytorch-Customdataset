'''
修改:
1. 对于已经split好train, val, test集的数据集
2. 增加metrics IOU
'''

import argparse
import logging
import os
import random
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import torchvision.transforms.functional as TF
from pathlib import Path
from torch import optim
from torch.utils.data import DataLoader, random_split
from tqdm import tqdm

import torchmetrics
from torch.utils.tensorboard import SummaryWriter

#import wandb
from evaluate import evaluate
from unet import UNet
from utils.data_loading import BasicDataset
from utils.dice_score import dice_loss

# dir_img = Path('./data/imgs/')
# dir_mask = Path('./data/masks/')

train_img_dir = Path('./favela_data/train/image/')
train_mask_dir = Path('./favela_data/train/mask_greyscale/')
val_img_dir = Path('./favela_data/val/image/')
val_mask_dir = Path('./favela_data/val/mask_greyscale/')
test_img_dir = Path('./favela_data/test/image/')
test_mask_dir = Path('./favela_data/test/mask_greyscale/')
dir_checkpoint = Path('./checkpoints/')

def split_and_create_data_loaders(dataset, val_percent, batch_size):
    # 1. Create dataset
    dataset = BasicDataset(dir_img, dir_mask, img_scale)
    # Split into train / validation partitions
    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(dataset, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    # Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=os.cpu_count(), pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=True, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)

    return train_loader, val_loader

def create_data_loaders_from_directories(train_img_dir, train_mask_dir, val_img_dir, val_mask_dir, test_img_dir, test_mask_dir, batch_size, img_scale):
    # 暂时忽略数据增强，在外部做好
    # Load datasets from directories
    train_dataset = BasicDataset(train_img_dir, train_mask_dir, img_scale)
    val_dataset = BasicDataset(val_img_dir, val_mask_dir, img_scale)
    test_dataset = BasicDataset(test_img_dir, test_mask_dir, img_scale)

    # Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=os.cpu_count(), pin_memory=True)
    train_loader = DataLoader(train_dataset, shuffle=True, **loader_args)
    val_loader = DataLoader(val_dataset, shuffle=False, drop_last=True, **loader_args)
    test_loader = DataLoader(test_dataset, shuffle=False, drop_last=True, **loader_args)

    return train_loader, val_loader, test_loader

def train_model(
        model,
        device,
        epochs: int = 5,
        batch_size: int = 1,
        learning_rate: float = 1e-5,
        val_percent: float = 0.1,
        save_checkpoint: bool = True,
        img_scale: float = 0.5,
        amp: bool = False,
        weight_decay: float = 1e-8,
        momentum: float = 0.999,
        gradient_clipping: float = 1.0,
        writer=None,  # added parameter
):
    # train_loader, val_loader = split_and_create_data_loaders(dataset, val_percent, batch_size)
    train_loader, val_loader, test_loader = create_data_loaders_from_directories(train_img_dir, train_mask_dir, val_img_dir, val_mask_dir, test_img_dir, test_mask_dir, batch_size, img_scale)
    n_train = len(train_loader.dataset)  
    n_val = len(val_loader.dataset)  

    # Initialize TensorBoard writer， added by ruyiyang
    writer = SummaryWriter(log_dir='runs/experiment1')

    # Initialize mean IoU metric, added by ruyiyang
    train_mean_iou = torchmetrics.JaccardIndex(task='binary',num_classes=model.n_classes).to(device) # adjust here
    val_mean_iou = torchmetrics.JaccardIndex(task='binary',num_classes=model.n_classes).to(device) # adjust here, 'multiclass' 还是'binary'
    print(model.n_classes)

    # 4. Set up the optimizer, the loss, the learning rate scheduler and the loss scaling for AMP
    optimizer = optim.RMSprop(model.parameters(),
                              lr=learning_rate, weight_decay=weight_decay, momentum=momentum, foreach=True)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=5)  # goal: maximize Dice score
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    criterion = nn.CrossEntropyLoss() if model.n_classes > 1 else nn.BCEWithLogitsLoss()
    global_step = 0

    # 5. Begin training
    for epoch in range(1, epochs + 1):
        model.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images, true_masks = batch['image'], batch['mask']

                assert images.shape[1] == model.n_channels, \
                    f'Network has been defined with {model.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32, memory_format=torch.channels_last)
                true_masks = true_masks.to(device=device, dtype=torch.long)

                with torch.autocast(device.type if device.type != 'mps' else 'cpu', enabled=amp):
                    masks_pred = model(images)
                    if model.n_classes == 1:
                        loss = criterion(masks_pred.squeeze(1), true_masks.float())
                        loss += dice_loss(F.sigmoid(masks_pred.squeeze(1)), true_masks.float(), multiclass=False)
                    else:
                        loss = criterion(masks_pred, true_masks)
                        loss += dice_loss(
                            F.softmax(masks_pred, dim=1).float(),
                            F.one_hot(true_masks, model.n_classes).permute(0, 3, 1, 2).float(),
                            multiclass=True
                        )

                optimizer.zero_grad(set_to_none=True)
                grad_scaler.scale(loss).backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clipping)
                grad_scaler.step(optimizer)
                grad_scaler.update()
                
                # Update mean IoU
                preds = torch.argmax(masks_pred, dim=1)
                train_mean_iou.update(preds, true_masks)

                pbar.update(images.shape[0])
                global_step += 1
                epoch_loss += loss.item()
                print(f"loss: {loss.item()}")

                # Evaluation round
                division_step = (n_train // (5 * batch_size))
                if division_step > 0:
                    if global_step % division_step == 0:
                        histograms = {}
                        for tag, value in model.named_parameters():
                            tag = tag.replace('/', '.')

                        val_score = evaluate(model, val_loader, device, amp)
                        scheduler.step(val_score)

        # Compute and log mean IoU for training
        train_iou = train_mean_iou.compute()
        writer.add_scalar('Train/Mean_IoU', train_iou, epoch)
        writer.add_scalar('Train/Loss', epoch_loss / len(train_loader), epoch)

        # Validation phase
        model.eval()
        val_loss = 0
        val_mean_iou.reset()
        with torch.no_grad():
            for batch in val_loader:
                images, true_masks = batch['image'], batch['mask']
                images = images.to(device=device, dtype=torch.float32, memory_format=torch.channels_last)
                true_masks = true_masks.to(device=device, dtype=torch.long)

                with torch.autocast(device.type if device.type != 'mps' else 'cpu', enabled=amp):
                    masks_pred = model(images)
                    if model.n_classes == 1:
                        loss = criterion(masks_pred.squeeze(1), true_masks.float())
                        loss += dice_loss(F.sigmoid(masks_pred.squeeze(1)), true_masks.float(), multiclass=False)
                    else:
                        loss = criterion(masks_pred, true_masks)
                        loss += dice_loss(
                            F.softmax(masks_pred, dim=1).float(),
                            F.one_hot(true_masks, model.n_classes).permute(0, 3, 1, 2).float(),
                            multiclass=True
                        )

                val_loss += loss.item()

                # Update mean IoU
                preds = torch.argmax(masks_pred, dim=1)
                val_mean_iou.update(preds, true_masks)

        # Compute and log mean IoU for validation
        val_iou = val_mean_iou.compute()
        writer.add_scalar('Validation/Mean_IoU', val_iou, epoch)
        writer.add_scalar('Validation/Loss', val_loss / len(val_loader), epoch)

        # Step the scheduler
        scheduler.step(val_iou)

        if save_checkpoint:
            Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
            state_dict = model.state_dict()
            state_dict['mask_values'] = dataset.mask_values
            torch.save(state_dict, str(dir_checkpoint / 'checkpoint_epoch{}.pth'.format(epoch)))
            logging.info(f'Checkpoint {epoch} saved!')
    
    # Close the TensorBoard writer
    writer.close()

def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=5, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=1, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=1e-5,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--scale', '-s', type=float, default=0.5, help='Downscaling factor of the images')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--amp', action='store_true', default=False, help='Use mixed precision')
    parser.add_argument('--bilinear', action='store_true', default=False, help='Use bilinear upsampling')
    parser.add_argument('--classes', '-c', type=int, default=2, help='Number of classes')
    parser.add_argument('--log-dir', type=str, default='lightning_logs/', help='Directory for TensorBoard logs') # added by ruyiyang

    return parser.parse_args()

def main():
    args = get_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    model = UNet(n_channels=3, n_classes=args.classes, bilinear=args.bilinear)
    model = model.to(memory_format=torch.channels_last)

    if args.load:
        state_dict = torch.load(args.load, map_location=device)
        del state_dict['mask_values']
        model.load_state_dict(state_dict)

    model.to(device=device)
    writer = SummaryWriter(log_dir=args.log_dir)  # added by ruyiyang

    train_model(
        model=model,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        device=device,
        img_scale=args.scale,
        val_percent=args.val / 100,
        amp=args.amp,
        writer=writer # added
    )

    writer.close() # added by ruyiyang

if __name__ == '__main__':
    main()