import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import gaussian_kde
from argparse import ArgumentParser
import os

def create_density_plot(npz_file, output_dir='./figs'):
    data = np.load(npz_file)
    l2_distances = data['l2_distance']

    fig, ax = plt.subplots(figsize=(10, 6))

    kde = gaussian_kde(l2_distances)
    x_range = np.linspace(l2_distances.min(), l2_distances.max(), 1000)

    ax.plot(x_range, kde(x_range))

    ax.set_xlabel('L2 Distance')
    ax.set_ylabel('Density')
    ax.set_title(f"Density Plot of L2 Distances")


    os.makedirs(output_dir, exist_ok=True)

    output_filename = os.path.join(output_dir, 'density_plot.png')
    fig.savefig(output_filename)
    plt.close(fig)

    print(f"Density plot saved to {output_filename}")

if __name__ == "__main__":

    parser = ArgumentParser()
    parser.add_argument('filename', type=str, help='Path to the NPZ file containing L2 distances')
    parser.add_argument('--output_dir', type=str, default='./figs', help='Directory to save the output plot')

    args = parser.parse_args()

    create_density_plot(args.filename, args.output_dir)